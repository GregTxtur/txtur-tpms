"""Single auth module for TPMS.

Everything about *who is logged in* lives here so it can be replaced by the
shared Microsoft 365 sign-on later without touching any page code.
Today: one shared app password from APP_PASSWORD in .env. If APP_PASSWORD is
unset, the gate is open (useful only on localhost during development).
"""
import os
from functools import wraps

from flask import (Blueprint, redirect, render_template, request, session,
                   url_for)

bp = Blueprint("auth", __name__)


def current_user():
    return session.get("user")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if os.environ.get("APP_PASSWORD") and not current_user():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == os.environ.get("APP_PASSWORD", ""):
            session["user"] = request.form.get("name", "").strip() or "plant"
            return redirect(request.args.get("next") or url_for("home"))
        error = "That password didn't match."
    return render_template("login.html", error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def init_auth(app):
    app.register_blueprint(bp)
    app.jinja_env.globals["current_user"] = current_user
