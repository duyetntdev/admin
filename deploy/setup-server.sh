#!/usr/bin/env bash
# Cài / cập nhật admin.duyetnt.com trên server. Chạy bằng root:  sudo bash setup-server.sh <thư mục chứa file>
# Chạy lại bao nhiêu lần cũng được (giữ nguyên dữ liệu).
set -euo pipefail
T="${1:?thiếu thư mục}"
DOMAIN=admin.duyetnt.com
APP=/opt/duyetnt-admin
SITE=/etc/nginx/sites-available/$DOMAIN
[ "$(id -u)" = 0 ] || { echo "Hãy chạy bằng sudo"; exit 1; }

# 1. code + service
install -d -m 755 $APP $APP/web
install -m 644 $T/admin.py $T/backfill.py $APP/
install -m 644 $T/web/index.html $APP/web/
changed=0
cmp -s $T/duyetnt-admin.service /etc/systemd/system/duyetnt-admin.service || {
  install -m 644 $T/duyetnt-admin.service /etc/systemd/system/; systemctl daemon-reload; changed=1; }
systemctl enable --quiet duyetnt-admin.service
systemctl restart duyetnt-admin.service
sleep 1
systemctl is-active --quiet duyetnt-admin.service || { journalctl -u duyetnt-admin -n 30 --no-pager; echo "!! Dịch vụ không chạy"; exit 1; }
echo "   Dịch vụ duyetnt-admin đang chạy (127.0.0.1:8095, nhận sự kiện 127.0.0.1:8096)"

# 2. mật khẩu (nginx basic auth)
install -m 640 -o root -g www-data $T/htpasswd /etc/nginx/duyetnt-admin.htpasswd

# 3. nginx + HTTPS (lần đầu)
if [ ! -f $SITE ]; then
  install -m 644 $T/nginx-admin.conf $SITE
  ln -sf $SITE /etc/nginx/sites-enabled/$DOMAIN
  nginx -t
  systemctl reload nginx
  echo "   Xin chứng chỉ HTTPS ..."
  certbot --nginx -d $DOMAIN --non-interactive --redirect --keep-until-expiring
fi
nginx -t 2>/dev/null && systemctl reload nginx

# 4. nhập dữ liệu cũ (lần đầu): các lần đăng nhập / đăng ký đã có trong 2 site
if [ ! -f $APP/.backfilled ]; then
  python3 $APP/backfill.py && touch $APP/.backfilled
fi
