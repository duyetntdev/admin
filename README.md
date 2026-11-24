# admin.duyetnt.com

Tổng hợp lượt truy cập của en.duyetnt.com và nihongo.duyetnt.com (username, ngày, giờ, thời lượng, thiết bị, IP).

- `server/admin.py`: dịch vụ Python (chỉ dùng thư viện chuẩn, SQLite). Cổng 8095 phục vụ giao diện web và API đọc, cổng 8096 nhận sự kiện từ 2 API học. Cả hai cổng chỉ nghe trên 127.0.0.1, và cổng 8096 không bao giờ được đưa ra nginx.
- `web/index.html`: giao diện.
- `deploy/`: systemd unit, site nginx (basic auth), `setup-server.sh`, `backfill.py` (nhập dữ liệu cũ một lần).
- 2 API học (`en-learn/server/app.py`, `nihongo-it/server/progress-api.mjs`) gửi sự kiện khi mở trang, đăng nhập, đăng ký hoặc lưu bài. Các sự kiện cách nhau dưới 30 phút được gộp thành 1 lượt.

## Deploy

```bash
./deploy.sh      # lần đầu tạo mật khẩu ở .admin-password (tài khoản: admin)
```

Đổi mật khẩu: xoá `.admin-password` rồi chạy lại `./deploy.sh`.
Dữ liệu trên server: `/var/lib/duyetnt-admin/admin.db`. Log: `journalctl -u duyetnt-admin -f`.

## Chạy thử trên máy

```bash
python3 server/admin.py --db /tmp/admin.db --static web
curl -d '{"site":"en","user":"bong","kind":"open"}' 127.0.0.1:8096/ingest
# mở http://127.0.0.1:8095
```
