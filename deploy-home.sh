#!/usr/bin/env bash
# Deploy trang chủ tĩnh lên https://duyetnt.com
# Dùng:  ./deploy-home.sh
#   SSH_KEY=~/keys/basion.pem ./deploy-home.sh
set -euo pipefail
cd "$(dirname "$0")"

SSH_KEY="${SSH_KEY:-../basion.pem}"
SERVER="${SERVER:-ubuntu@ec2-13-228-49-233.ap-southeast-1.compute.amazonaws.com}"
DOMAIN=duyetnt.com
[ -f "$SSH_KEY" ] || { echo "❌ Không thấy khoá SSH: $SSH_KEY"; exit 1; }
[ -f homepage/index.html ] || { echo "❌ Thiếu homepage/index.html"; exit 1; }

echo "📦 Đang upload lên $SERVER ..."
tar czf - -C homepage index.html -C ../deploy nginx-home.conf | ssh -i "$SSH_KEY" -o ConnectTimeout=15 "$SERVER" '
  set -e
  T=$(mktemp -d); tar xzf - -C $T
  W=/var/www/duyetnt-home
  # Giải nén vào thư mục tạm rồi tráo đổi, để web không bao giờ ở trạng thái dở dang
  sudo rm -rf $W.new $W.old; sudo mkdir -p $W.new
  sudo install -m 644 -o root -g root $T/index.html $W.new/index.html
  sudo chmod 755 $W.new
  [ -d $W ] && sudo mv $W $W.old
  sudo mv $W.new $W; sudo rm -rf $W.old

  # Cấu hình nginx: giữ bản gốc (site Docker cũ) một lần, nếu nginx -t lỗi thì khôi phục
  SITE=/etc/nginx/sites-available/duyetnt.com
  [ -f $SITE.bak-docker ] || sudo cp $SITE $SITE.bak-docker
  if ! sudo cmp -s $T/nginx-home.conf $SITE; then
    sudo cp $SITE $SITE.prev
    sudo install -m 644 $T/nginx-home.conf $SITE
    if ! sudo nginx -t 2>/dev/null; then
      sudo cp $SITE.prev $SITE; echo "❌ nginx -t lỗi, đã khôi phục cấu hình cũ"; sudo nginx -t; exit 1
    fi
    sudo systemctl reload nginx
  fi
  rm -rf $T
'

echo "🔎 Kiểm tra https://$DOMAIN ..."
c1=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/")
c2=$(curl -s -o /dev/null -w '%{http_code}' "http://$DOMAIN/")
curl -s "https://$DOMAIN/" | grep -q 'Bé Vui Học\|class="owl"' || { echo "❌ Trang trả về không phải homepage mới"; exit 1; }
echo "   https → $c1, http → $c2"
[ "$c1" = 200 ] && [ "$c2" = 301 ] || { echo "❌ Kết quả kiểm tra không đúng"; exit 1; }
echo "✅ Đã deploy: https://$DOMAIN"
