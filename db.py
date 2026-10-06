"""Database access for TPMS: one connection per request, plus the migration runner."""
import glob
import os

import psycopg
from flask import current_app, g
from psycopg.rows import dict_row

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
_LOCK_KEY = 8003001  # any constant; keeps two gunicorn workers from migrating at once


def get_db():
    if "db" not in g:
        g.db = psycopg.connect(current_app.config["DATABASE_URL"], row_factory=dict_row)
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.rollback()  # anything not explicitly committed is discarded
        conn.close()


def query(sql, params=None):
    return get_db().execute(sql, params).fetchall()


def one(sql, params=None):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=None):
    return get_db().execute(sql, params)


def commit():
    get_db().commit()


def rollback():
    get_db().rollback()


def run_migrations(url):
    """Apply migrations/NNN_*.sql in order, once each. Returns the names applied."""
    applied = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                "create table if not exists schema_migrations ("
                "name text primary key, applied_at timestamptz not null default now())"
            )
            done = {r[0] for r in conn.execute("select name from schema_migrations")}
            for path in sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))):
                name = os.path.basename(path)
                if name in done:
                    continue
                with open(path, encoding="utf-8") as fh:
                    sql = fh.read()
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute("insert into schema_migrations (name) values (%s)", (name,))
                applied.append(name)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied
