#!/usr/bin/env python3
"""Nhập một lần các lượt truy cập đã có sẵn trong 2 site vào admin (chạy bằng root trên server, qua setup-server.sh).

en.duyetnt.com:      users.created → đăng ký, tokens.created → đăng nhập (vé còn hiệu lực)
nihongo.duyetnt.com: created → đăng ký, updated → lần lưu tiến độ cuối
Đọc bản sao của dữ liệu, không đụng tới file thật.
"""
import glob, json, os, shutil, sqlite3, tempfile, urllib.request

EN_DB = '/var/lib/en-learn/en-learn.db'
NI_JSON = '/var/lib/nihongo-progress/users.json'
INGEST = 'http://127.0.0.1:8096/ingest'

ev = []
if os.path.exists(EN_DB):
    with tempfile.TemporaryDirectory() as d:
        for f in glob.glob(EN_DB + '*'):
            shutil.copy(f, d)
        c = sqlite3.connect(os.path.join(d, os.path.basename(EN_DB)))
        created = {}
        for key, name, cr in c.execute('SELECT key, name, created FROM users'):
            created[key] = cr
            ev.append(('en', name, 'register', cr))
        for name, key, cr in c.execute('SELECT u.name, t.key, t.created FROM tokens t JOIN users u ON u.key=t.key'):
            if abs(cr - created.get(key, 0)) > 5:          # vé tạo lúc đăng ký đã tính ở trên
                ev.append(('en', name, 'login', cr))
        c.close()
if os.path.exists(NI_JSON):
    for name, u in json.load(open(NI_JSON)).get('users', {}).items():
        cr, up = int(u.get('created', 0) / 1000), int(u.get('updated', 0) / 1000)
        if cr:
            ev.append(('nihongo', name, 'register', cr))
        if up > cr + 60:
            ev.append(('nihongo', name, 'save', up))

ev.sort(key=lambda e: e[3])
for site, user, kind, ts in ev:
    body = json.dumps({'site': site, 'user': user, 'kind': kind, 'ts': ts}).encode()
    urllib.request.urlopen(urllib.request.Request(INGEST, body, {'Content-Type': 'application/json'}), timeout=5).close()
print(f'   Đã nhập {len(ev)} sự kiện cũ')
