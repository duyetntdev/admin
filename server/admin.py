#!/usr/bin/env python3
"""Trang quản trị admin.duyetnt.com – tổng hợp lượt truy cập của en.duyetnt.com và nihongo.duyetnt.com.

Hai cổng (đều chỉ nghe trên 127.0.0.1):
  --port 8095         giao diện web + API đọc (nginx chuyển admin.duyetnt.com tới đây, có mật khẩu)
  --ingest-port 8096  nhận sự kiện từ 2 API học (KHÔNG bao giờ đưa ra ngoài qua nginx)

Nhận sự kiện:
  POST /ingest {site: en|nihongo, user, kind: open|login|register|save, ip?, ua?, ts?}
  Các sự kiện của cùng một người trên cùng site cách nhau < 30 phút được gộp thành 1 lượt truy cập.

API đọc:
  GET /api/visits?from=YYYY-MM-DD&to=YYYY-MM-DD&site=&user=   (ngày theo giờ Việt Nam)
      → {visits: [{id, site, user, kind, start, last, hits, ip, ua}], truncated}

Quản lý hồ sơ en.duyetnt.com (chuyển tiếp tới cổng quản trị nội bộ của en-learn, mặc định 127.0.0.1:8091):
  GET  /api/en/users                       → {users: [{name, hasPin, locked, levels, updated}]}
  POST /api/en/reset-pin {name}            → {ok, name}   (bắt buộc header X-Admin: 1 để chặn CSRF)

Chạy thử trên máy:
  python3 server/admin.py --db /tmp/admin.db --static web
  # mở http://127.0.0.1:8095, gửi thử: curl -d '{"site":"en","user":"bong","kind":"open"}' 127.0.0.1:8096/ingest
"""
import argparse, datetime, json, os, re, sqlite3, threading, time, urllib.error, urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SITES = {'en', 'nihongo'}
KINDS = {'open', 'login', 'register', 'save'}
USER_RE = re.compile(r'^[A-Za-z0-9]{1,30}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
GAP = 30 * 60                       # khoảng nghỉ tối đa trong cùng một lượt truy cập (giây)
MAX_ROWS = 5000
VN = datetime.timezone(datetime.timedelta(hours=7))
EN_ADMIN = 'http://127.0.0.1:8091'          # cổng quản trị nội bộ của en-learn (đổi bằng --en-admin)

db_lock = threading.Lock()
db = None


def open_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    d = sqlite3.connect(path, check_same_thread=False)
    d.execute('PRAGMA journal_mode=WAL')
    d.execute('''CREATE TABLE IF NOT EXISTS visits (
        id INTEGER PRIMARY KEY,
        site TEXT NOT NULL,
        user TEXT NOT NULL,            -- tên hiển thị
        ukey TEXT NOT NULL,            -- tên viết thường để gộp
        kind TEXT NOT NULL,            -- sự kiện mở đầu lượt truy cập
        start INTEGER NOT NULL,        -- unix time
        last INTEGER NOT NULL,
        hits INTEGER NOT NULL DEFAULT 1,
        ip TEXT NOT NULL DEFAULT '',
        ua TEXT NOT NULL DEFAULT '')''')
    d.execute('CREATE INDEX IF NOT EXISTS visits_user ON visits(site, ukey, last)')
    d.execute('CREATE INDEX IF NOT EXISTS visits_start ON visits(start)')
    d.commit()
    return d


def record(site, user, kind, ts, ip, ua):
    ukey = user.lower()
    with db_lock:
        r = db.execute('SELECT id, last FROM visits WHERE site=? AND ukey=? ORDER BY last DESC LIMIT 1',
                       (site, ukey)).fetchone()
        # login/register luôn là lượt mới (người dùng vừa nhập tên + PIN); open/save nối vào lượt đang mở
        if r and kind in ('open', 'save') and ts - r[1] < GAP:
            db.execute('UPDATE visits SET last=MAX(last, ?), hits=hits+1, user=? WHERE id=?', (ts, user, r[0]))
        else:
            db.execute('INSERT INTO visits(site,user,ukey,kind,start,last,ip,ua) VALUES(?,?,?,?,?,?,?,?)',
                       (site, user, ukey, kind, ts, ts, ip, ua))
        db.commit()


def day_start(s):
    y, m, d = map(int, s.split('-'))
    return int(datetime.datetime(y, m, d, tzinfo=VN).timestamp())


class Base(BaseHTTPRequestHandler):
    server_version = 'duyetnt-admin'

    def log_message(self, fmt, *args):
        print('%s %s %s' % (self.server.server_port, self.command, urlparse(self.path).path), flush=True)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


class Ingest(Base):
    def do_POST(self):
        if urlparse(self.path).path != '/ingest':
            return self.send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 4096:
                raise ValueError
            d = json.loads(self.rfile.read(n) or b'{}')
            site, user, kind = d.get('site'), d.get('user'), d.get('kind')
            if site not in SITES or kind not in KINDS or not isinstance(user, str) or not USER_RE.match(user):
                raise ValueError
            ts = d.get('ts')
            ts = int(ts) if isinstance(ts, (int, float)) and 1_600_000_000 < ts <= time.time() + 60 else int(time.time())
            ip = str(d.get('ip') or '')[:64]
            ua = str(d.get('ua') or '')[:300]
        except (ValueError, TypeError):
            return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'bad request'})
        record(site, user, kind, ts, ip, ua)
        self.send_json(HTTPStatus.OK, {'ok': True})


class Web(Base):
    static_dir = None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/visits':
            return self.visits(parse_qs(u.query))
        if u.path == '/api/en/users':
            return self.en_admin('GET', '/users')
        if u.path in ('/', '/index.html'):
            return self.file('index.html', 'text/html; charset=utf-8')
        self.send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})

    def do_POST(self):
        if urlparse(self.path).path != '/api/en/reset-pin':
            return self.send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})
        # Basic auth được trình duyệt tự gửi kèm, nên form của trang khác cũng POST được tới đây.
        # Header tự đặt buộc trình duyệt hỏi CORS trước (mà ta không cho) → chỉ trang này gọi được.
        if self.headers.get('X-Admin') != '1':
            return self.send_json(HTTPStatus.FORBIDDEN, {'error': 'forbidden'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 4096:
                raise ValueError
            name = json.loads(self.rfile.read(n) or b'{}').get('name')
            if not isinstance(name, str) or not USER_RE.match(name):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'Tên không hợp lệ'})
        self.en_admin('POST', '/reset-pin', {'name': name})

    def en_admin(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(EN_ADMIN + path, data, {'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status, out = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, out = e.code, e.read()
        except OSError:
            return self.send_json(HTTPStatus.BAD_GATEWAY, {'error': 'Không kết nối được máy chủ en.duyetnt.com'})
        try:
            return self.send_json(status, json.loads(out))
        except ValueError:
            return self.send_json(HTTPStatus.BAD_GATEWAY, {'error': 'Máy chủ en.duyetnt.com trả lời không hợp lệ'})

    def file(self, name, ctype):
        with open(os.path.join(self.static_dir, name), 'rb') as f:
            body = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def visits(self, q):
        g = lambda k: (q.get(k) or [''])[0]
        today = datetime.datetime.now(VN).date()
        f, t = g('from'), g('to')
        f = f if DATE_RE.match(f) else (today - datetime.timedelta(days=6)).isoformat()
        t = t if DATE_RE.match(t) else today.isoformat()
        try:
            lo, hi = day_start(f), day_start(t) + 86400
        except ValueError:
            return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'ngày không hợp lệ'})
        sql, args = 'SELECT id,site,user,kind,start,last,hits,ip,ua FROM visits WHERE start>=? AND start<?', [lo, hi]
        if g('site') in SITES:
            sql += ' AND site=?'; args.append(g('site'))
        if g('user'):
            sql += ' AND ukey LIKE ?'; args.append('%' + g('user').lower().replace('%', '').replace('_', '') + '%')
        sql += ' ORDER BY start DESC LIMIT ?'; args.append(MAX_ROWS + 1)
        with db_lock:
            rows = db.execute(sql, args).fetchall()
        cols = ['id', 'site', 'user', 'kind', 'start', 'last', 'hits', 'ip', 'ua']
        self.send_json(HTTPStatus.OK, {'from': f, 'to': t, 'truncated': len(rows) > MAX_ROWS,
                                       'visits': [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]})


def main():
    global db, EN_ADMIN
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8095)
    ap.add_argument('--ingest-port', type=int, default=8096)
    ap.add_argument('--db', default='/var/lib/duyetnt-admin/admin.db')
    ap.add_argument('--en-admin', default=EN_ADMIN, help='cổng quản trị nội bộ của en-learn')
    ap.add_argument('--static', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'web'))
    a = ap.parse_args()
    EN_ADMIN = a.en_admin.rstrip('/')
    db = open_db(a.db)
    Web.static_dir = os.path.abspath(a.static)
    ing = ThreadingHTTPServer((a.host, a.ingest_port), Ingest)
    threading.Thread(target=ing.serve_forever, daemon=True).start()
    print(f'admin web {a.host}:{a.port}, ingest {a.host}:{a.ingest_port}, db {a.db}', flush=True)
    ThreadingHTTPServer((a.host, a.port), Web).serve_forever()


if __name__ == '__main__':
    main()
