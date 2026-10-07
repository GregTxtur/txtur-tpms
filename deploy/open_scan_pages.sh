#!/usr/bin/env bash
# Lets the TPMS phone scan pages past the Microsoft sign-on.
#
# What it changes: adds two rules to the web server's TPMS site so that /s/... (the pages a phone
# opens when it scans a code) and /static/... (their stylesheet) are served without the Microsoft
# gate. On those paths the gate's identity headers are blanked, so nothing sent from a phone can
# pass for an office sign-in. Every other TPMS page stays behind the gate, exactly as before.
#
# This is a change to the server's sign-on rules, so it is run by a person, once:
#
#     sudo bash /opt/tpms/deploy/open_scan_pages.sh
#
# It is safe to run again (it does nothing the second time). It keeps a copy of the old rules and
# puts them back by itself if the web server rejects the new ones. To undo it later, copy the
# backup it names over the site file and run: sudo systemctl reload nginx
set -euo pipefail

SITE=/etc/nginx/sites-enabled/tpms
MARK="tpms-scan-pages"

[ "$(id -u)" = "0" ] || { echo "Run this with sudo."; exit 1; }
[ -e "$SITE" ] || { echo "Cannot find $SITE. Nothing changed."; exit 1; }
CONF=$(readlink -f "$SITE")

if grep -q "$MARK" "$CONF"; then
    echo "The scan pages are already open. Nothing changed."
else
    grep -qE '^[[:space:]]*location / \{' "$CONF" || { echo "Cannot find the 'location / {' rule in $CONF. Nothing changed."; exit 1; }
    BACKUP=/var/backups/tpms-nginx-before-scan-pages-$(date +%Y%m%d-%H%M%S).conf
    cp -p "$CONF" "$BACKUP"
    BLOCK=$(mktemp)
    cat > "$BLOCK" <<'NGINX'
    # tpms-scan-pages: phone scan pages and their stylesheet skip the Microsoft gate; TPMS asks for
    # name + PIN itself. The gate's headers are blanked so they cannot be forged from a phone.
    location = /s { return 302 /s/me; }
    location ~ ^/(s|static)/ {
        proxy_pass http://127.0.0.1:8003;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Auth-Request-Email "";
        proxy_set_header X-Auth-Request-User "";
        proxy_set_header X-Auth-Request-Groups "";
    }
NGINX
    # Insert the block just above the first "location / {" rule (the gated catch-all).
    awk -v block="$BLOCK" '
        /^[[:space:]]*location \/ \{/ && !done { while ((getline line < block) > 0) print line; done = 1 }
        { print }' "$BACKUP" > "$CONF"
    rm -f "$BLOCK"
    if nginx -t 2> /tmp/tpms-nginx-test.txt; then
        systemctl reload nginx
        echo "Done. The old rules are saved at $BACKUP"
    else
        cp -p "$BACKUP" "$CONF"
        cat /tmp/tpms-nginx-test.txt
        echo "The web server rejected the new rules, so the old ones were put back. Nothing changed."
        exit 1
    fi
fi

echo
echo "Checks (run from this server, with no sign-in):"
curl -s -o /dev/null -w "  phone page   /s/who   -> %{http_code}   (200 means phones can reach it)\n" https://tpms.txturtools.com/s/who
curl -s -o /dev/null -w "  stylesheet   /static  -> %{http_code}   (200)\n" https://tpms.txturtools.com/static/tpms.css
curl -s -o /dev/null -w "  office page  /team    -> %{http_code}   (302 means it still asks for Microsoft sign-in)\n" https://tpms.txturtools.com/team
curl -s -o /dev/null -w "  office page  /setup   -> %{http_code}   (302)\n" https://tpms.txturtools.com/setup
curl -s -o /dev/null -H "X-Auth-Request-Email: someone@txtur.com" -w "  office page with a forged sign-in header -> %{http_code}   (302: the forgery is ignored)\n" https://tpms.txturtools.com/team
