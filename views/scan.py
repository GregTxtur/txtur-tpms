"""The pages a team member's phone lands on when it scans a code.

These pages do not use the office sign-on. A phone says who it is once (name, then PIN) and is
remembered; after that every scan is tied to that team member. The codes themselves are unguessable.

Every scan that changes something answers with a redirect to /s/done, a read-only page. That way the
address left in the phone's browser is never the action itself, so a reload, the Back button or a tab
the phone reopens later cannot scan anyone on or off a second time.
"""
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Blueprint, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash

import db
import floor
from util import norm_ws

bp = Blueprint("scan", __name__, url_prefix="/s")

FREE_PIN_TRIES = 5          # wrong tries before the first lock
LOCK_MINUTES = 5            # first lock; doubles with each further wrong try
MAX_LOCK_MINUTES = 240


def _pin_stamp(member):
    """Changes whenever the PIN does, so a new PIN signs every remembered phone out."""
    return member["pin_hash"][-16:]


def _member():
    member_id = session.get("tm")
    if not member_id:
        return None
    member = db.one("select * from team_member where id = %s and active", (member_id,))
    if not member or session.get("tmv") != _pin_stamp(member):
        return None
    return member


def _safe_next(target):
    return target if target and target.startswith("/s/") and "//" not in target[1:] else url_for("scan.me")


def needs_member(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        member = _member()
        if not member:
            # After signing in, a scan is retried; a button press (POST) just lands on My Work.
            target = request.full_path.rstrip("?") if request.method == "GET" else url_for("scan.me")
            return redirect(url_for("scan.who", next=target))
        if floor.close_stale(db.get_db()):
            db.commit()
        return view(member, *args, **kwargs)
    return wrapped


def _count(text):
    """A sheet count as typed on a phone: 0 to 9999, plain digits only. None if it is anything else."""
    text = norm_ws(text)
    return int(text) if re.fullmatch(r"[0-9]{1,4}", text) else None


def _plural(n, word="sheet"):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _message(tone, title, lines=(), co=None, actions=(), member=None):
    return render_template("scan/message.html", tone=tone, title=title, lines=lines, co=co, actions=actions,
                           member=member)


def _done(tone, title, lines=(), co_id=None, actions=()):
    """Hand the outcome of an action to /s/done and send the phone there."""
    session["done"] = {"tone": tone, "title": title, "lines": list(lines), "co_id": co_id,
                       "actions": [list(a) for a in actions]}
    return redirect(url_for("scan.done"))


@bp.route("/done")
@needs_member
def done(member):
    result = session.pop("done", None)
    if not result:
        return redirect(url_for("scan.me"))
    co = floor.get_cut_order(db.get_db(), result["co_id"]) if result.get("co_id") else None
    return _message(result["tone"], result["title"], result["lines"], co=co, actions=result["actions"],
                    member=member)


# ---- who is holding this phone

@bp.route("/who")
def who():
    members = db.query("select id, name from team_member where active order by lower(name)")
    return render_template("scan/who.html", members=members, next=_safe_next(request.args.get("next")))


@bp.route("/pin/<int:member_id>", methods=["GET", "POST"])
def pin(member_id):
    target = _safe_next(request.values.get("next"))
    member = db.one("select * from team_member where id = %s and active", (member_id,))
    if not member:
        return redirect(url_for("scan.who", next=target))
    error = None
    now = datetime.now(timezone.utc)
    locked = bool(member["locked_until"] and member["locked_until"] > now)
    if not member["pin_hash"]:
        error = "No PIN has been set for you yet. Ask your supervisor."
    elif locked:
        error = "Too many wrong tries. Wait a while, or ask your supervisor to reset your PIN."
    elif request.method == "POST":
        if check_password_hash(member["pin_hash"], norm_ws(request.form.get("pin"))):
            db.execute("update team_member set failed_pins = 0, locked_until = null where id = %s", (member_id,))
            db.commit()
            session["tm"] = member_id
            session["tmv"] = _pin_stamp(member)
            session.permanent = True
            return redirect(target)
        # Count the miss in the database (two phones guessing at once both count). The count is only
        # cleared by a correct PIN or a supervisor reset, and each miss past the free tries doubles the wait.
        tries = db.one("update team_member set failed_pins = failed_pins + 1 where id = %s returning failed_pins",
                       (member_id,))["failed_pins"]
        if tries >= FREE_PIN_TRIES:
            minutes = min(LOCK_MINUTES * 2 ** min(tries - FREE_PIN_TRIES, 10), MAX_LOCK_MINUTES)
            db.execute("update team_member set locked_until = %s where id = %s",
                       (now + timedelta(minutes=minutes), member_id))
            error, locked = f"Too many wrong tries. Wait {minutes} minutes, or ask your supervisor.", True
        else:
            error = "That PIN is not right."
        db.commit()
    return render_template("scan/pin.html", candidate=member, error=error, next=target,
                           blocked=bool(not member["pin_hash"] or locked))


@bp.route("/switch", methods=["POST"])
def switch():
    session.pop("tm", None)
    session.pop("tmv", None)
    session.pop("done", None)
    return redirect(url_for("scan.who"))


# ---- my work

@bp.route("/")
def index():
    return redirect(url_for("scan.me"))


@bp.route("/me")
@needs_member
def me(member):
    sessions = floor.open_sessions(db.get_db(), member["id"])
    return render_template("scan/me.html", member=member, sessions=sessions, error=request.args.get("e"))


@bp.route("/find", methods=["POST"])
@needs_member
def find(member):
    """For a phone whose camera will not read the code: type the cut order number instead."""
    number = floor.parse_co_number(request.form.get("number"))
    row = db.one("select token from cut_order where id = %s", (number,)) if number and number < 2 ** 31 else None
    if not row:
        return redirect(url_for("scan.me", e="No cut order has that number."))
    direction = "off" if request.form.get("direction") == "off" else "on"
    return redirect(url_for("scan.cut_order_" + direction, token=row["token"]))


@bp.route("/tap/<int:session_id>", methods=["POST"])
@needs_member
def tap(member, session_id):
    s = db.one("select id from scan_session where id = %s and team_member_id = %s and ended_at is null "
               "and cut_order_id is not null", (session_id, member["id"]))
    if s:
        if request.form.get("delta") == "-1":
            db.execute("delete from sheet_tap where id = (select max(id) from sheet_tap where session_id = %s)",
                       (session_id,))
        else:
            db.execute("insert into sheet_tap (session_id) values (%s)", (session_id,))
        db.commit()
    return redirect(url_for("scan.me") + f"#s{session_id}")


# ---- cut orders

@bp.route("/c/<token>/on", methods=["GET", "POST"])
@needs_member
def cut_order_on(member, token):
    conn = db.get_db()
    co = floor.get_cut_order(conn, token=token)
    if not co:
        return _message("bad", "Code not recognised", ["This is not a TPMS cut order code."], member=member)
    if co["status"] == "filed":
        return _message("bad", f"{co['number']} is not released yet",
                        ["Ask the supervisor to release it before cutting."], co=co, member=member)
    if co["status"] == "cancelled":
        return _message("bad", f"{co['number']} was cancelled", ["Do not cut this. Check with the supervisor."],
                        co=co, member=member)
    if not co["item_id"]:
        # Without a plywood there is nothing to deduct the sheets from, so the count would vanish.
        return _message("bad", f"{co['number']} has no plywood set",
                        ["Ask the supervisor to set the plywood on this cut order before cutting."],
                        co=co, member=member)
    already = db.one("select id from scan_session where team_member_id = %s and cut_order_id = %s "
                     "and ended_at is null", (member["id"], co["id"]))
    if already:
        return _message("ok", f"You are already on {co['number']}", ["Nothing changed."], co=co, member=member,
                        actions=[("My work", url_for("scan.me"))])
    confirmed = request.method == "POST" and request.form.get("confirm") == "1"
    if not confirmed:
        mostly = floor.setting_int(conn, "mostly_cut_pct")
        if co["status"] == "complete" or (co["sheets_open"] == 0 and co["sheets_required"]):
            return render_template("scan/confirm_on.html", member=member, co=co,
                                   title=f"{co['number']} was already scanned complete",
                                   line="Only scan on if more really has to be cut.")
        if co["pct_cut"] is not None and co["pct_cut"] >= mostly:
            return render_template("scan/confirm_on.html", member=member, co=co,
                                   title=f"{co['number']} is nearly all cut",
                                   line=f"Only {_plural(co['sheets_open'])} still open.")
    session_id, was_on = floor.scan_on_cut_order(conn, member["id"], co)
    credit = db.one("select fixed_minutes from scan_session where id = %s", (session_id,))["fixed_minutes"]
    db.commit()
    lines = [f"Changeover: {credit} minutes earned."] if credit and not was_on else []
    return _done("ok", f"You are ON {co['number']}", lines, co_id=co["id"],
                 actions=[("My work and sheet counter", url_for("scan.me") + f"#s{session_id}")])


@bp.route("/c/<token>/off", methods=["GET", "POST"])
@needs_member
def cut_order_off(member, token):
    conn = db.get_db()
    co = floor.get_cut_order(conn, token=token)
    if not co:
        return _message("bad", "Code not recognised", ["This is not a TPMS cut order code."], member=member)
    s = db.one("select s.*, (select count(*) from sheet_tap t where t.session_id = s.id) as taps "
               "from scan_session s where team_member_id = %s and cut_order_id = %s and ended_at is null",
               (member["id"], co["id"]))
    if not s:
        return _not_on(member, co, token)
    error, confirm_value = None, ""
    value = str(s["taps"]) if s["taps"] else ""
    if request.method == "POST":
        value = norm_ws(request.form.get("sheets"))
        sheets = _count(value)
        if sheets is None:
            error = "Enter how many sheets you cut this time, as a number. Enter 0 if none."
        else:
            over = co["sheets_open"] is not None and sheets > co["sheets_open"]
            # The second press only counts for the very number that was warned about.
            if over and request.form.get("confirm") != str(sheets):
                confirm_value = str(sheets)
                error = (f"That is {sheets - co['sheets_open']} more than the {co['sheets_open']} that were open. "
                         "If that is right, press the button again.")
            else:
                done_co = floor.scan_off_cut_order(conn, s["id"], sheets, member["name"])
                db.commit()
                if done_co is None:  # a second press arrived after the first already closed it
                    return _not_on(member, co, token)
                lines = [f"{_plural(sheets)} recorded."]
                if done_co["status"] == "complete":
                    lines.append("This cut order is now complete.")
                elif done_co["sheets_open"] is not None:
                    lines.append(f"{_plural(done_co['sheets_open'])} still open.")
                return _done("ok", f"You are OFF {co['number']}", lines, co_id=co["id"],
                             actions=[("My work", url_for("scan.me"))])
    return render_template("scan/off.html", member=member, co=co, s=s, value=value, error=error,
                           confirm_value=confirm_value)


def _not_on(member, co, token):
    """Scan off with no open scan. If they only just scanned off, say so rather than invite a new scan on."""
    recent = db.one("select sheets from scan_session where team_member_id = %s and cut_order_id = %s "
                    "and closed_by = 'self' and ended_at > now() - interval '2 minutes' "
                    "order by ended_at desc limit 1", (member["id"], co["id"]))
    if recent:
        return _message("ok", f"You are already OFF {co['number']}",
                        [f"{_plural(recent['sheets'] or 0)} recorded a moment ago. Nothing changed."],
                        co=co, member=member, actions=[("My work", url_for("scan.me"))])
    return _message("warn", f"You are not scanned onto {co['number']}", ["So there is nothing to scan off."],
                    co=co, member=member,
                    actions=[("My work", url_for("scan.me")),
                             ("Scan on instead", url_for("scan.cut_order_on", token=token))])


# ---- off order activities (the poster)

def _activity_and_machine(token, machine_code):
    activity = db.one("select * from activity where token = %s", (token,))
    machine = db.one("select * from machine where upper(code) = %s", (machine_code.upper(),))
    return activity, machine


@bp.route("/a/<token>/<machine_code>/on")
@needs_member
def activity_on(member, token, machine_code):
    activity, machine = _activity_and_machine(token, machine_code)
    if not activity or not machine:
        return _message("bad", "Code not recognised", ["This is not a TPMS poster code."], member=member)
    if not activity["active"]:
        return _message("bad", f"{activity['name']} is no longer in use",
                        ["This code was retired. Ask the supervisor for a new poster."], member=member)
    _, already = floor.scan_on_activity(db.get_db(), member["id"], activity, machine["id"])
    db.commit()
    if already:
        return _message("ok", f"You are already on {activity['name']}", [f"At {machine['name']}. Nothing changed."],
                        member=member, actions=[("My work", url_for("scan.me"))])
    if activity["earns"]:
        line = (f"Earns {activity['earned_minutes']} minutes." if activity["earned_minutes"]
                else "No standard time is set for this yet.")
    else:
        line = "This does not earn minutes. The time is recorded with its reason."
    return _done("ok", f"You are ON {activity['name']}", [f"At {machine['name']}.", line, "Scan off when you are done."],
                 actions=[("My work", url_for("scan.me"))])


@bp.route("/a/<token>/<machine_code>/off")
@needs_member
def activity_off(member, token, machine_code):
    activity, machine = _activity_and_machine(token, machine_code)
    if not activity or not machine:
        return _message("bad", "Code not recognised", ["This is not a TPMS poster code."], member=member)
    row = floor.scan_off_activity(db.get_db(), member["id"], activity["id"], machine["id"])
    db.commit()
    if not row:
        return _message("warn", f"You were not on {activity['name']}", [f"At {machine['name']}. Nothing changed."],
                        member=member, actions=[("My work", url_for("scan.me"))])
    minutes = round((row["ended_at"] - row["started_at"]).total_seconds() / 60)
    return _done("ok", f"You are OFF {activity['name']}", [f"{_plural(minutes, 'minute')} at {machine['name']}."],
                 actions=[("My work", url_for("scan.me"))])


@bp.route("/end/<int:session_id>", methods=["POST"])
@needs_member
def end_activity(member, session_id):
    """End an activity from My Work, for when the poster is not in reach."""
    db.execute("update scan_session set ended_at = now(), closed_by = 'self' where id = %s and team_member_id = %s "
               "and activity_id is not null and ended_at is null", (session_id, member["id"]))
    db.commit()
    return redirect(url_for("scan.me"))


# ---- scan off everything (lunch, end of shift)

@bp.route("/off-all", methods=["GET", "POST"])
@needs_member
def off_all(member):
    conn = db.get_db()
    sessions = floor.open_sessions(conn, member["id"])
    if not sessions:
        return _message("ok", "You are not scanned onto anything", [], member=member)
    values, error, confirm_value = {}, None, ""
    for s in sessions:
        if s["cut_order_id"]:
            values[s["id"]] = str(s["taps"]) if s["taps"] else ""
    if request.method == "POST":
        for sid in values:
            values[sid] = norm_ws(request.form.get(f"sheets_{sid}"))
        counts = {sid: _count(v) for sid, v in values.items()}
        signature = ",".join(f"{sid}:{counts[sid]}" for sid in sorted(counts))
        over = [s["cut_order"]["number"] for s in sessions if s["cut_order_id"] and counts[s["id"]] is not None
                and s["cut_order"]["sheets_open"] is not None and counts[s["id"]] > s["cut_order"]["sheets_open"]]
        if any(c is None for c in counts.values()):
            error = "Enter the sheets cut for every cut order, as a number. Enter 0 if none."
        elif over and request.form.get("confirm") != signature:
            confirm_value = signature
            error = ("More than was open on " + ", ".join(over) + ". If the numbers are right, press the button again.")
        else:
            lines = []
            for s in sessions:
                if s["cut_order_id"]:
                    done_co = floor.scan_off_cut_order(conn, s["id"], counts[s["id"]], member["name"])
                    if done_co:
                        lines.append(f"{done_co['number']}: {_plural(counts[s['id']])} recorded.")
                else:
                    db.execute("update scan_session set ended_at = now(), closed_by = 'self' where id = %s "
                               "and ended_at is null", (s["id"],))
                    lines.append(f"{s['activity_name']}: ended.")
            db.commit()
            return _done("ok", "You are OFF everything", lines)
    return render_template("scan/off_all.html", member=member, sessions=sessions, values=values, error=error,
                           confirm_value=confirm_value)
