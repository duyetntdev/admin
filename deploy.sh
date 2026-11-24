#!/usr/bin/env bash
# Deploy trang quản trị lên https://admin.duyetnt.com
# Dùng:  ./deploy.sh          (lần đầu tự tạo mật khẩu, lưu ở .admin-password)
#   SSH_KEY=~/keys/basion.pem ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

SSH_KEY="${SSH_KEY:-../basion.pem}"
SERVER="${SERVER:-ubuntu@ec2-13-228-49-233.ap-southeast-1.compute.amazonaws.com}"
DOMAIN=admin.duyetnt.com
ADMIN_USER=admin
[ -f "$SSH_KEY" ] || { echo "❌ Không thấy khoá SSH: $SSH_KEY"; exit 1; }
python3 -m py_compile server/admin.py deploy/backfill.py

# Mật khẩu đăng nhập trang admin (đổi: xoá file .admin-password rồi chạy lại ./deploy.sh)
PWFILE=.admin-password
if [ ! -s "$PWFILE" ]; then
  (umask 077; openssl rand -base64 18 | tr -d '/+=' | cut -c1-20 > "$PWFILE")
  echo "🔑 Đã tạo mật khẩu mới cho $ADMIN_USER (lưu trong admin/$PWFILE)"
fi
PW=$(cat "$PWFILE")
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/web"
cp server/admin.py deploy/*.py deploy/*.sh deploy/*.conf deploy/*.service "$STAGE/"; cp web/index.html "$STAGE/web/"
printf '%s:%s\n' "$ADMIN_USER" "$(openssl passwd -apr1 "$PW")" > "$STAGE/htpasswd"

echo "📦 Đang cài lên $SERVER ..."
tar czf - -C "$STAGE" . | ssh -i "$SSH_KEY" -o ConnectTimeout=15 "$SERVER" '
  set -e
  T=/tmp/duyetnt-admin-setup; rm -rf $T; mkdir -p $T; tar xzf - -C $T
  sudo bash $T/setup-server.sh $T
  rm -rf $T
'

echo "🔎 Kiểm tra https://$DOMAIN ..."
c1=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/")
c2=$(curl -s -o /dev/null -w '%{http_code}' -u "$ADMIN_USER:$PW" "https://$DOMAIN/")
c3=$(curl -s -o /dev/null -w '%{http_code}' -u "$ADMIN_USER:$PW" "https://$DOMAIN/api/visits")
c4=$(curl -s -o /dev/null -w '%{http_code}' -u "$ADMIN_USER:$PW" -d '{}' "https://$DOMAIN/ingest")
echo "   không mật khẩu → $c1, có mật khẩu → $c2, /api/visits → $c3, POST /ingest → $c4"
[ "$c1" = 401 ] && [ "$c2" = 200 ] && [ "$c3" = 200 ] && [ "$c4" = 403 ] || { echo "❌ Kết quả kiểm tra không đúng"; exit 1; }
echo "✅ Đã deploy: https://$DOMAIN  (tài khoản: $ADMIN_USER, mật khẩu trong admin/$PWFILE)"
