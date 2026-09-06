"""TPMS — Txtur Plywood Management System. Skeleton release 0.0.1."""
import os
import socket
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify
import psycopg

from auth import init_auth, login_required

APP_VERSION = "0.0.1-skeleton"


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
    init_auth(app)

    def db_status():
        url = app.config["DATABASE_URL"]
        if not url:
            return {"ok": False, "detail": "DATABASE_URL not set"}
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("select version(), current_database(), current_user")
                    ver, db, user = cur.fetchone()
            return {"ok": True, "detail": f"{db} as {user} — {ver.split(',')[0]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc).strip()}

    @app.route("/")
    @login_required
    def home():
        return render_template(
            "home.html",
            version=APP_VERSION,
            host=socket.gethostname(),
            db=db_status(),
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )

    @app.route("/health")
    def health():
        db = db_status()
        return jsonify(app="tpms", version=APP_VERSION, db_ok=db["ok"]), (200 if db["ok"] else 503)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8003, debug=True)
