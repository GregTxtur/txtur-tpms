#!/usr/bin/env bash
# Bring the server copy of TPMS up to date: pull main, install new packages, restart, check health.
# The app applies any new database migrations itself when it starts.
set -euo pipefail
cd /opt/tpms
git pull --ff-only
venv/bin/pip install -q -r requirements.txt
sudo systemctl restart tpms
sleep 4
curl -s http://127.0.0.1:8003/health
echo
