# TPMS — Txtur Plywood Management System

Skeleton release. Runs at https://tpms.txturtools.com on TxturSalesTools,
localhost port 8003, systemd unit `tpms`, database `tpms_db` / role `tpms_user`.

Local dev: `python -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/python app.py`

Auth: everything lives in `auth/`. Replace that package when the shared M365 sign-on exists.
