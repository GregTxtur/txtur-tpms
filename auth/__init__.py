"""Single auth module for TPMS.

Identity comes from the shared Microsoft 365 sign-on gate (oauth2-proxy in
front of nginx). nginx only forwards a request to this app after the gate has
approved it, and it stamps these headers on every request:

    X-Auth-Request-Email   greg@txtur.com
    X-Auth-Request-User    (Entra object id)
    X-Auth-Request-Groups  comma-separated Entra group ids

The app binds to 127.0.0.1 and is reachable only through nginx, so the headers
can be trusted. There is no password anywhere in this app any more.

Per-app roles: map Entra group ids (or emails) to roles in ROLE_GROUPS below.
Everyone who passes the gate gets "user"; anyone in an admin group gets
"admin" as well. Check with `has_role("admin")` in page code or
`{% if has_role("admin") %}` in templates.

Local development (no gate): set TPMS_DEV_USER=you@txtur.com in the
environment and every request is treated as that person.
"""
import os
from functools import wraps

from flask import Blueprint, redirect, request

bp = Blueprint("auth", __name__)

SIGNIN = "https://txturtools.com/oauth2/start?rd="
SIGNOUT = "https://txturtools.com/oauth2/sign_out?rd="

# Entra group object ids -> TPMS role. Add rows as groups are created.
ROLE_GROUPS = {
    # "00000000-0000-0000-0000-000000000000": "admin",
}
# Emails that are admins regardless of group (handy before groups exist).
ROLE_EMAILS = {
    "greg@txtur.com": "admin",
}


def current_user():
    """Dict with email/name/groups/roles for the signed-in person, or None."""
    email = request.headers.get("X-Auth-Request-Email") or os.environ.get("TPMS_DEV_USER")
    if not email:
        return None
    email = email.lower()
    groups = [g for g in request.headers.get("X-Auth-Request-Groups", "").split(",") if g]
    roles = {"user"}
    for g in groups:
        if g in ROLE_GROUPS:
            roles.add(ROLE_GROUPS[g])
    if email in ROLE_EMAILS:
        roles.add(ROLE_EMAILS[email])
    return {
        "email": email,
        "name": email.split("@")[0].replace(".", " ").title(),
        "id": request.headers.get("X-Auth-Request-User", ""),
        "groups": groups,
        "roles": sorted(roles),
    }


def has_role(role):
    u = current_user()
    return bool(u) and role in u["roles"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(SIGNIN + request.url)
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user():
                return redirect(SIGNIN + request.url)
            if not has_role(role):
                return ("You're signed in, but this page needs the %s role." % role, 403)
            return view(*args, **kwargs)
        return wrapped
    return deco


@bp.route("/login")
def login():
    return redirect(SIGNIN + (request.args.get("next") or request.url_root))


@bp.route("/logout")
def logout():
    return redirect(SIGNOUT + request.url_root)


def init_auth(app):
    app.register_blueprint(bp)
    app.jinja_env.globals["current_user"] = current_user
    app.jinja_env.globals["has_role"] = has_role
