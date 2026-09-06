#!/usr/bin/env bash
# One-shot deploy of the TPMS skeleton on TxturSalesTools, following txturtools-new-app-setup.md.
# Safe to re-run: every step checks before it changes anything.
set -euo pipefail
APP=tpms; PORT=8003; HOST=tpms.txturtools.com; DIR=/opt/$APP
SHOP_PASSWORD="${1:-}"; CERT_EMAIL="${2:-}"
[ -z "$SHOP_PASSWORD" ] && { echo "usage: bash deploy_tpms.sh SHOP_PASSWORD [cert-email]"; exit 1; }
[ -f "$HOME/tpms.tar.gz" ] || { echo "tpms.tar.gz not found in $HOME"; exit 1; }

echo "== 0. what is already here"
sudo ss -tlnp | grep -E ":(80|443|5432|80[0-9][0-9]) " || true
ls /etc/nginx/sites-enabled 2>/dev/null || echo "(nginx not installed yet)"
if sudo ss -tlnp | grep -q ":$PORT " && ! systemctl is-active --quiet $APP; then
  echo "PORT $PORT is taken by something other than $APP — pick another port"; exit 1; fi

echo "== 1. base packages"
sudo apt-get install -y -qq python3-venv python3-pip nginx certbot python3-certbot-nginx postgresql postgresql-contrib >/dev/null

echo "== 2. database"
DBPASS=$(openssl rand -hex 24)
if sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='tpms_user'" | grep -q 1; then
  echo "role tpms_user exists; keeping existing .env password"
  DBPASS=$(grep -oP 'postgresql://tpms_user:\K[^@]+' $DIR/.env 2>/dev/null || true)
else
  sudo -u postgres psql -qc "create role tpms_user with login password '$DBPASS'"
fi
sudo -u postgres psql -tAc "select 1 from pg_database where datname='tpms_db'" | grep -q 1 \
  || sudo -u postgres psql -qc "create database tpms_db owner tpms_user"

echo "== 3. app files"
sudo mkdir -p $DIR && sudo chown ubuntu:ubuntu $DIR
tar -xzf "$HOME/tpms.tar.gz" -C $DIR
cd $DIR
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
cat > .env <<ENV
DATABASE_URL=postgresql://tpms_user:$DBPASS@localhost:5432/tpms_db
SECRET_KEY=$(openssl rand -hex 32)
APP_PASSWORD=$SHOP_PASSWORD
FLASK_ENV=production
ENV
chmod 600 .env
fi

echo "== 4. systemd"
sudo tee /etc/systemd/system/$APP.service >/dev/null <<UNIT
[Unit]
Description=TPMS — Txtur plywood management (Txtur tools)
After=network.target postgresql.service

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=$DIR/venv/bin/gunicorn --workers 2 --bind 127.0.0.1:$PORT app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now $APP
sudo systemctl restart $APP
sleep 2
curl -s http://127.0.0.1:$PORT/health; echo

echo "== 5. nginx"
sudo tee /etc/nginx/sites-available/$APP >/dev/null <<NG
server {
    listen 80;
    server_name $HOST;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    client_max_body_size 25M;
}
NG
sudo ln -sf /etc/nginx/sites-available/$APP /etc/nginx/sites-enabled/$APP
sudo nginx -t && sudo systemctl reload nginx

echo "== 6. TLS (needs the A record for $HOST already pointing here)"
if getent hosts $HOST | grep -q "$(curl -s https://api.ipify.org)"; then
  sudo certbot --nginx -d $HOST --non-interactive --agree-tos --redirect ${CERT_EMAIL:+-m $CERT_EMAIL} ${CERT_EMAIL:---register-unsafely-without-email} || echo "certbot failed; re-run: sudo certbot --nginx -d $HOST"
else
  echo "DNS for $HOST does not resolve to this box yet — skipping certbot. Re-run this script after the A record propagates."
fi
echo "== done. https://$HOST  (localhost port $PORT, unit $APP, db tpms_db)"
