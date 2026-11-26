# admin.duyetnt.com

Tổng hợp lượt truy cập của en.duyetnt.com và nihongo.duyetnt.com (username, ngày, giờ, thời lượng, thiết bị, IP).

- `server/admin.py`: dịch vụ Python (chỉ dùng thư viện chuẩn, SQLite). Cổng 8095 phục vụ giao diện web và API đọc, cổng 8096 nhận sự kiện từ 2 API học. Cả hai cổng chỉ nghe trên 127.0.0.1, và cổng 8096 không bao giờ được đưa ra nginx.
- `web/index.html`: giao diện, gồm cả mục **Reset mã PIN** cho hồ sơ en.duyetnt.com (gọi cổng quản trị nội bộ 127.0.0.1:8091 của en-learn; cổng này cũng không bao giờ được đưa ra nginx).
- `homepage/index.html`: trang chủ tĩnh của duyetnt.com, giới thiệu en.duyetnt.com và nihongo.duyetnt.com (một file HTML, không cần build).
- `deploy/`: systemd unit, site nginx (basic auth), `setup-server.sh`, `backfill.py` (nhập dữ liệu cũ một lần).
- 2 API học (`en-learn/server/app.py`, `nihongo-it/server/progress-api.mjs`) gửi sự kiện khi mở trang, đăng nhập, đăng ký hoặc lưu bài. Các sự kiện cách nhau dưới 30 phút được gộp thành 1 lượt.
- Thống kê hành vi en.duyetnt.com: trình duyệt của bé gửi `POST /api/track` (thời gian ở từng phần, bắt đầu / làm xong luyện tập, kiểm tra, trò chơi; không ghi câu trả lời), en-learn kiểm tra rồi chuyển sang `POST /events` (cổng 8096), lưu ở bảng `events`. Trang admin đọc qua `GET /api/behavior` (tab "Hành vi học sinh").

## Deploy

```bash
./deploy.sh      # lần đầu tạo mật khẩu ở .admin-password (tài khoản: admin)
./deploy-home.sh # trang chủ duyetnt.com (homepage/index.html → /var/www/duyetnt-home, nginx: deploy/nginx-home.conf)
```

Cấu hình nginx cũ của duyetnt.com (khi còn chạy Docker) được giữ ở `/etc/nginx/sites-available/duyetnt.com.bak-docker`.

Đổi mật khẩu: xoá `.admin-password` rồi chạy lại `./deploy.sh`.
Dữ liệu trên server: `/var/lib/duyetnt-admin/admin.db`. Log: `journalctl -u duyetnt-admin -f`.

## Chạy thử trên máy

```bash
python3 server/admin.py --db /tmp/admin.db --static web
curl -d '{"site":"en","user":"bong","kind":"open"}' 127.0.0.1:8096/ingest
# mở http://127.0.0.1:8095
```
