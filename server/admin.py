#!/usr/bin/env python3
"""Trang quản trị admin.duyetnt.com – tổng hợp lượt truy cập của en.duyetnt.com và nihongo.duyetnt.com.

Hai cổng (đều chỉ nghe trên 127.0.0.1):
  --port 8095         giao diện web + API đọc (nginx chuyển admin.duyetnt.com tới đây, có mật khẩu)
  --ingest-port 8096  nhận sự kiện từ 2 API học (KHÔNG bao giờ đưa ra ngoài qua nginx)

Nhận sự kiện:
  POST /ingest {site: en|nihongo, user, kind: open|login|register|save, ip?, ua?, ts?}
  Các sự kiện của cùng một người trên cùng site cách nhau < 30 phút được gộp thành 1 lượt truy cập.
  POST /events {site, user, ip?, ua?, events: [{e: view|start|done, a: khu vực, t, d?, l?, lv?, x?, s?, n?, ok?}]}
  Thống kê hành vi do en-learn chuyển sang (POST /api/track của en.duyetnt.com). Cũng tính là đang hoạt động.

API đọc:
  GET /api/visits?from=YYYY-MM-DD&to=YYYY-MM-DD&site=&user=   (ngày theo giờ Việt Nam)
      → {visits: [{id, site, user, kind, start, last, hits, ip, ua}], truncated}
  GET /api/behavior?from=&to=&user=   thống kê hành vi en.duyetnt.com trong khoảng ngày
      → {areas, acts, lessons, islands, daily, hours, users}  (xem hàm behavior)

Quản lý hồ sơ en.duyetnt.com (chuyển tiếp tới cổng quản trị nội bộ của en-learn, mặc định 127.0.0.1:8091):
  GET  /api/en/users                       → {users: [{name, hasPin, locked, levels, updated}]}
  POST /api/en/reset-pin {name}            → {ok, name}   (bắt buộc header X-Admin: 1 để chặn CSRF)
  GET  /api/en/packs                       → {packs: [{id, title, ..., enabled, kids, lessons}]}  gói nội dung mới
  POST /api/en/packs/toggle {id, on}       → {ok, id, enabled}   (bắt buộc header X-Admin: 1)

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
    d.execute('''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        site TEXT NOT NULL,
        user TEXT NOT NULL,
        ukey TEXT NOT NULL,
        ts INTEGER NOT NULL,           -- unix time
        ev TEXT NOT NULL,              -- view | start | done
        area TEXT NOT NULL,            -- khu vực / hoạt động (lesson.vocab, castle, test, ...)
        dur INTEGER,                   -- view: số giây ở màn hình
        lesson INTEGER, level INTEGER, -- số thứ tự bài (0..), cấp
        x TEXT,                        -- mã đảo (trò kho báu)
        score INTEGER, total INTEGER, ok INTEGER)''')
    d.execute('CREATE INDEX IF NOT EXISTS events_ts ON events(site, ts)')
    d.execute('CREATE INDEX IF NOT EXISTS events_user ON events(site, ukey, ts)')
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


EV_TYPES = {'view', 'start', 'done'}
AREA_RE = re.compile(r'^[a-z]{1,20}(\.[a-z]{1,20})?$')
X_RE = re.compile(r'^[a-z]{1,20}$')


def record_events(site, user, events, ip, ua):
    now, rows = int(time.time()), []
    num = lambda v, lo, hi: v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None
    for e in events[:100]:
        if not isinstance(e, dict) or e.get('e') not in EV_TYPES or not isinstance(e.get('a'), str) or not AREA_RE.match(e['a']):
            continue
        ts = num(e.get('t'), now - 7 * 86400, now + 60) or now
        x = e.get('x') if isinstance(e.get('x'), str) and X_RE.match(e['x']) else None
        rows.append((site, user, user.lower(), ts, e['e'], e['a'], num(e.get('d'), 1, 3600), num(e.get('l'), 0, 999),
                     num(e.get('lv'), 1, 9), x, num(e.get('s'), 0, 999), num(e.get('n'), 1, 999), num(e.get('ok'), 0, 1)))
    if not rows:
        return 0
    with db_lock:
        db.executemany('INSERT INTO events(site,user,ukey,ts,ev,area,dur,lesson,level,x,score,total,ok) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        db.commit()
    record(site, user, 'save', max(r[3] for r in rows), ip, ua)     # bé đang hoạt động → kéo dài lượt truy cập
    return len(rows)


LEARN = "area LIKE 'lesson.%'"
GAME = "area IN ('castle','treasure','island','chest','explore','expedition','diploma')"


def behavior(site, lo, hi, user):
    """Thống kê hành vi trong [lo, hi). Thời gian tính bằng giây, ngày / giờ theo giờ Việt Nam."""
    w, a = 'site=? AND ts>=? AND ts<?', [site, lo, hi]
    if user:
        w += ' AND ukey LIKE ?'; a.append('%' + user.lower().replace('%', '').replace('_', '') + '%')
    vn = "(ts + 25200)"
    q = lambda sql, extra=(): db.execute(sql, a + list(extra)).fetchall()
    with db_lock:
        areas = q(f"SELECT area, COUNT(*), COUNT(DISTINCT ukey), SUM(dur) FROM events WHERE {w} AND ev='view' GROUP BY area")
        acts = q(f"""SELECT area, SUM(ev='start'), SUM(ev='done'), COUNT(DISTINCT CASE WHEN ev='start' THEN ukey END),
                   AVG(CASE WHEN ev='done' AND total>0 THEN 100.0*score/total END), SUM(ev='done' AND ok=1)
                   FROM events WHERE {w} AND ev IN ('start','done') GROUP BY area""")
        lessons = q(f"""SELECT lesson, SUM(CASE WHEN ev='view' THEN dur ELSE 0 END), COUNT(DISTINCT ukey),
                   SUM(ev='done' AND area='test'), SUM(ev='done' AND area='test' AND ok=1), SUM(ev='done' AND area='practice')
                   FROM events WHERE {w} AND lesson IS NOT NULL AND (area LIKE 'lesson.%' OR area IN ('test','practice'))
                   GROUP BY lesson""")
        islands = q(f"""SELECT x, SUM(ev='start'), SUM(ev='done'), SUM(CASE WHEN ev='view' THEN dur ELSE 0 END),
                   AVG(CASE WHEN ev='done' AND total>0 THEN total-score END)
                   FROM events WHERE {w} AND area='island' AND x IS NOT NULL GROUP BY x""")
        daily = q(f"""SELECT date({vn}, 'unixepoch'), SUM(CASE WHEN {LEARN} THEN dur ELSE 0 END),
                   SUM(CASE WHEN {GAME} THEN dur ELSE 0 END), SUM(dur), COUNT(DISTINCT ukey)
                   FROM events WHERE {w} AND ev='view' GROUP BY 1 ORDER BY 1""")
        hours = q(f"SELECT CAST(strftime('%H', {vn}, 'unixepoch') AS INTEGER), SUM(dur) FROM events WHERE {w} AND ev='view' GROUP BY 1")
        users = q(f"""SELECT MAX(user), SUM(CASE WHEN ev='view' THEN dur ELSE 0 END),
                   SUM(CASE WHEN ev='view' AND {LEARN} THEN dur ELSE 0 END), SUM(CASE WHEN ev='view' AND {GAME} THEN dur ELSE 0 END),
                   SUM(ev='done' AND area='test'), SUM(ev='done' AND area='test' AND ok=1), MAX(ts), COUNT(DISTINCT date({vn}, 'unixepoch'))
                   FROM events WHERE {w} GROUP BY ukey ORDER BY 2 DESC LIMIT 1000""")
        fav = dict(q(f"""SELECT ukey, area FROM (SELECT ukey, area, SUM(dur) s,
                   ROW_NUMBER() OVER (PARTITION BY ukey ORDER BY SUM(dur) DESC) r
                   FROM events WHERE {w} AND ev='view' AND area NOT IN ('home','fame') GROUP BY ukey, area) WHERE r=1"""))
    return {
        'areas': [{'area': r[0], 'views': r[1], 'users': r[2], 'sec': r[3] or 0} for r in areas],
        'acts': [{'act': r[0], 'starts': r[1] or 0, 'dones': r[2] or 0, 'users': r[3] or 0,
                  'avg': round(r[4]) if r[4] is not None else None, 'passed': r[5] or 0} for r in acts],
        'lessons': [{'lesson': r[0], 'sec': r[1] or 0, 'users': r[2], 'tests': r[3] or 0, 'passed': r[4] or 0,
                     'practices': r[5] or 0} for r in lessons],
        'islands': [{'x': r[0], 'starts': r[1] or 0, 'dones': r[2] or 0, 'sec': r[3] or 0,
                     'wrong': round(r[4], 1) if r[4] is not None else None} for r in islands],
        'daily': [{'day': r[0], 'learn': r[1] or 0, 'game': r[2] or 0, 'all': r[3] or 0, 'users': r[4]} for r in daily],
        'hours': [{'h': r[0], 'sec': r[1] or 0} for r in hours],
        'users': [{'user': r[0], 'sec': r[1] or 0, 'learn': r[2] or 0, 'game': r[3] or 0, 'tests': r[4] or 0,
                   'passed': r[5] or 0, 'last': r[6], 'days': r[7], 'fav': fav.get(r[0].lower())} for r in users],
    }


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
        path = urlparse(self.path).path
        if path == '/events':
            return self.events()
        if path != '/ingest':
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

    def events(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 64 * 1024:
                raise ValueError
            d = json.loads(self.rfile.read(n) or b'{}')
            site, user, evs = d.get('site'), d.get('user'), d.get('events')
            if site not in SITES or not isinstance(user, str) or not USER_RE.match(user) or not isinstance(evs, list):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'bad request'})
        k = record_events(site, user, evs, str(d.get('ip') or '')[:64], str(d.get('ua') or '')[:300])
        self.send_json(HTTPStatus.OK, {'ok': True, 'n': k})


class Web(Base):
    static_dir = None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/visits':
            return self.visits(parse_qs(u.query))
        if u.path == '/api/behavior':
            return self.behavior(parse_qs(u.query))
        if u.path == '/api/en/users':
            return self.en_admin('GET', '/users')
        if u.path == '/api/en/packs':
            return self.en_admin('GET', '/packs')
        if u.path in ('/', '/index.html'):
            return self.file('index.html', 'text/html; charset=utf-8')
        self.send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/en/reset-pin', '/api/en/packs/toggle'):
            return self.send_json(HTTPStatus.NOT_FOUND, {'error': 'not found'})
        # Basic auth được trình duyệt tự gửi kèm, nên form của trang khác cũng POST được tới đây.
        # Header tự đặt buộc trình duyệt hỏi CORS trước (mà ta không cho) → chỉ trang này gọi được.
        if self.headers.get('X-Admin') != '1':
            return self.send_json(HTTPStatus.FORBIDDEN, {'error': 'forbidden'})
        if path == '/api/en/packs/toggle':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n > 4096:
                    raise ValueError
                d = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(d.get('id'), str) or not re.match(r'^[a-z0-9-]{1,30}$', d['id']) or not isinstance(d.get('on'), bool):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'Dữ liệu không hợp lệ'})
            return self.en_admin('POST', '/packs/toggle', {'id': d['id'], 'on': d['on']})
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

    def date_range(self, q):
        g = lambda k: (q.get(k) or [''])[0]
        today = datetime.datetime.now(VN).date()
        f, t = g('from'), g('to')
        f = f if DATE_RE.match(f) else (today - datetime.timedelta(days=6)).isoformat()
        t = t if DATE_RE.match(t) else today.isoformat()
        return f, t, day_start(f), day_start(t) + 86400

    def behavior(self, q):
        try:
            f, t, lo, hi = self.date_range(q)
        except ValueError:
            return self.send_json(HTTPStatus.BAD_REQUEST, {'error': 'ngày không hợp lệ'})
        data = behavior('en', lo, hi, (q.get('user') or [''])[0])
        self.send_json(HTTPStatus.OK, dict(data, **{'from': f, 'to': t}))

    def visits(self, q):
        g = lambda k: (q.get(k) or [''])[0]
        try:
            f, t, lo, hi = self.date_range(q)
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
