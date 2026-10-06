"""TPMS — Txtur Plywood Management System. Build 1: item master, program list, inventory."""
import logging
import os
import socket
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template
import psycopg

import db
from auth import init_auth, login_required
from util import PLANT_TZ, fmt_secs, item_label

APP_VERSION = "0.1.0"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MIGRATION_ERROR"] = ""
    init_auth(app)
    app.teardown_appcontext(db.close_db)

    # Bring the database up to date on start, so the deploy loop stays "pull, restart".
    if app.config["DATABASE_URL"] and os.environ.get("TPMS_SKIP_MIGRATE") != "1":
        try:
            applied = db.run_migrations(app.config["DATABASE_URL"])
            if applied:
                app.logger.warning("applied migrations: %s", ", ".join(applied))
        except Exception as exc:  # noqa: BLE001
            logging.exception("migration failed")
            app.config["MIGRATION_ERROR"] = str(exc).strip()

    @app.template_filter("localtime")
    def localtime(value, fmt="%m/%d/%y %I:%M %p"):
        if not value:
            return ""
        return value.astimezone(PLANT_TZ).strftime(fmt)

    @app.template_filter("mmss")
    def mmss(value):
        return fmt_secs(value)

    @app.template_filter("num")
    def num(value, places=0):
        if value is None:
            return ""
        return f"{value:,.{places}f}"

    app.jinja_env.globals["item_label"] = item_label
    app.jinja_env.globals["app_version"] = APP_VERSION

    def db_status():
        url = app.config["DATABASE_URL"]
        if not url:
            return {"ok": False, "detail": "DATABASE_URL not set"}
        if app.config["MIGRATION_ERROR"]:
            return {"ok": False, "detail": "database update failed: " + app.config["MIGRATION_ERROR"]}
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("select version(), current_database(), current_user")
                    ver, name, user = cur.fetchone()
            return {"ok": True, "detail": f"{name} as {user} — {ver.split(',')[0]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc).strip()}

    @app.route("/")
    @login_required
    def home():
        status = db_status()
        stats = None
        if status["ok"]:
            stats = db.one("""
                select (select count(*) from item where active) as item_count,
                       (select coalesce(sum(qty_sheets), 0) from inventory_txn) as sheets,
                       (select count(*) from program) as programs,
                       (select count(*) from program p where status <> 'retired'
                          and not exists (select 1 from program_time t where t.program_id = p.id)) as no_time,
                       (select count(*) from inventory_txn
                         where created_at > now() - interval '7 days') as txns_week
            """)
        return render_template(
            "home.html", version=APP_VERSION, host=socket.gethostname(), db=status, stats=stats,
            now=datetime.now(timezone.utc).astimezone(PLANT_TZ).strftime("%m/%d/%y %I:%M %p"),
        )

    @app.route("/health")
    def health():
        status = db_status()
        return jsonify(app="tpms", version=APP_VERSION, db_ok=status["ok"]), (200 if status["ok"] else 503)

    from views.items import bp as items_bp
    from views.inventory import bp as inventory_bp
    from views.programs import bp as programs_bp
    for blueprint in (items_bp, inventory_bp, programs_bp):
        app.register_blueprint(blueprint)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8003, debug=True)
