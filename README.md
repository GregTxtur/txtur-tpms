# TPMS — Txtur Plywood Management System

Runs at https://tpms.txturtools.com on TxturSalesTools: localhost port 8003, systemd unit `tpms`,
database `tpms_db` / role `tpms_user`. GitHub `main` is the single source of truth; `/opt/tpms` on the
server is a checkout of it.

## What is here (build 1)

| Screen | Path | What it does |
|---|---|---|
| Material board | `/board` | On-hand sheets and packs per plywood, reorder flags, value |
| Receive | `/receive` | Book a delivery in packs or sheets |
| Count / adjust | `/adjust` | Physical counts and corrections, each with a reason |
| Ledger | `/ledger` | Every inventory movement; filters and Excel download |
| Program list | `/programs` | The CNC program list: search, filter, edit, router times |
| Plywood setup | `/items`, `/setup/types`, `/setup/suppliers` | Item master, plywood types, suppliers |
| Load workbook | `/import` | One-time load of the legacy "Active Orders on Plywood" workbook |

Words: stock is always counted in **sheets**. A **pack** is the bundle plywood arrives in.
**Unit** only ever means an order unit (one chair's worth of parts).

## Rules the code follows

- On-hand is always `SUM(inventory_txn.qty_sheets)`. No balance is ever stored.
- Database changes go in `migrations/NNN_name.sql`, applied in order, once each, when the app starts.
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
