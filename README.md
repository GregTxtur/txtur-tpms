# TPMS — Txtur Plywood Management System

Runs at https://tpms.txturtools.com on TxturSalesTools: localhost port 8003, systemd unit `tpms`,
database `tpms_db` / role `tpms_user`. GitHub `main` is the single source of truth; `/opt/tpms` on the
server is a checkout of it.

## What is here

| Screen | Path | What it does |
|---|---|---|
| Cut orders | `/cut-orders` | The supervisor's short form, filed and released lists, the cover sheet with SCAN ON / SCAN OFF codes |
| Cutting now | `/cutting` | Who is scanned onto what, live |
| Daily minutes | `/minutes` | Per team member: on the clock against earned, with Excel download |
| Material board | `/board` | On-hand sheets and packs, demand from released and filed cut orders, reorder flags |
| Receive, Count / adjust, Ledger | `/receive`, `/adjust`, `/ledger` | Stock in, corrections, and every movement |
| Program list | `/programs` | The CNC program list: search, filter, edit, router times |
| Setup | `/setup` | Team members and PINs, off order activities, standards and posters, fix-up, plywood, the workbook |
| Phone pages | `/s/...` | What a team member's phone opens when it scans a code |

Words: stock is always counted in **sheets**. A **pack** is the bundle plywood arrives in.
**Unit** only ever means an order unit (one chair's worth of parts).

## Scanning and sign-on

Office pages are behind the shared Microsoft 365 gate. The phone pages under `/s/` are not: a team member's
phone says who it is once (name, then PIN) and is remembered. For that to work, nginx must let `/s/` and
`/static/` through without the gate, and must blank the gate's headers on those paths so nothing typed into
a phone can pass for an office sign-in:

    location = /s { return 302 /s/me; }
    location ~ ^/(s|static)/ {
        proxy_pass http://127.0.0.1:8003;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Auth-Request-Email "";
        proxy_set_header X-Auth-Request-User "";
        proxy_set_header X-Auth-Request-Groups "";
    }

Match `/s/` exactly as above. A looser `/s` would also open `/setup`.

Earned minutes come from three places: sheets cut x the program's router time, a changeover standard once
per cut order (not when the same program runs back to back on a router), and a fixed standard for each
earning off order activity. The rules live in `floor.py`. `tests/e2e.py` runs the whole flow (cut orders, scanning, minutes,
fix-up, stock arithmetic) against a throwaway database; run it before each release.

## Rules the code follows

- On-hand is always `SUM(inventory_txn.qty_sheets)`. No balance is ever stored. Sheets cut on a cut order are
  likewise always the sum of its scans.
- Database changes go in `migrations/NNN_name.sql` (or `NNN_name.py` with a `run(conn)` when SQL cannot do the
  job), applied in order, once each, when the app starts.
  Never edit a migration that has already run on the server; add a new one.
- Everything about who is signed in lives in `auth/`. Pages never import from it directly; they use
  `guard.require_login()` and `guard.user_label()`, which work with both the shared-password module and the
  Microsoft 365 gate module that the live server runs.
- Business data (the workbook, program list, orders) never goes in this repository. It is loaded through `/import`.
- The server runs Python 3.10 and PostgreSQL 14. Test against those.

## Deploy

On the server:

    bash /opt/tpms/update.sh

That pulls `main`, installs any new packages, restarts the service (which applies new migrations) and
prints the health check.

## Local development

    python -m venv venv
    venv/bin/pip install -r requirements.txt
    DATABASE_URL=postgresql://tpms_user:...@localhost/tpms_db venv/bin/python app.py

With `APP_PASSWORD` unset the sign-in gate is open, which is only for a developer's own machine.
