"""Supervisor screens for the floor: team members, off order activities, posters,
who is cutting now, daily minutes, and fixing up forgotten scans."""
import io
import re
from datetime import datetime, timedelta

import psycopg
from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, url_for)
from openpyxl import Workbook
from werkzeug.security import generate_password_hash

import db
import floor
import qr
from guard import require_login
from util import PLANT_TZ, norm_ws

bp = Blueprint("shop", __name__)
bp.before_request(require_login)


@bp.before_request
def _tidy():
    if floor.close_stale(db.get_db()):
        db.commit()


@bp.route("/setup")
def setup():
    counts = db.one("""
        select (select count(*) from team_member where active) as team,
               (select count(*) from activity where active) as activities,
               (select count(*) from scan_session where needs_review) as fixups,
               (select count(*) from item where active) as items
    """)
    return render_template("shop/setup.html", counts=counts)


# ---- team members

def _valid_pin(pin):
    return bool(re.fullmatch(r"\d{4,6}", pin))


@bp.route("/team", methods=["GET", "POST"])
def team():
    if request.method == "POST":
        action = request.form.get("action")
        member_id = request.form.get("id", type=int)
        name = norm_ws(request.form.get("name"))
        pin = norm_ws(request.form.get("pin"))
        try:
            if action == "add":
                if not name or not _valid_pin(pin):
                    flash("Enter a name and a PIN of 4 to 6 digits.", "bad")
                else:
                    db.execute("insert into team_member (name, pin_hash) values (%s, %s)",
                               (name, generate_password_hash(pin)))
                    flash(f"Added {name}.", "ok")
            elif action == "pin" and member_id:
                if not _valid_pin(pin):
                    flash("A PIN is 4 to 6 digits.", "bad")
                else:
                    db.execute("update team_member set pin_hash = %s, failed_pins = 0, locked_until = null "
                               "where id = %s", (generate_password_hash(pin), member_id))
                    flash("PIN changed.", "ok")
            elif action == "rename" and member_id and name:
                db.execute("update team_member set name = %s where id = %s", (name, member_id))
                flash(f"Renamed to {name}.", "ok")
            elif action == "toggle" and member_id:
                db.execute("update team_member set active = not active where id = %s", (member_id,))
            db.commit()
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash(f"There is already a team member called {name}.", "bad")
        return redirect(url_for("shop.team"))
    rows = db.query("""
        select tm.*, (tm.locked_until is not null and tm.locked_until > now()) as locked,
               (select max(started_at) from scan_session s where s.team_member_id = tm.id) as last_scan,
               (select count(*) from scan_session s where s.team_member_id = tm.id and s.ended_at is null) as on_now
          from team_member tm order by tm.active desc, lower(tm.name)
    """)
    return render_template("shop/team.html", rows=rows)


# ---- off order activities and the standards

@bp.route("/activities", methods=["GET", "POST"])
def activities():
    conn = db.get_db()
    if request.method == "POST":
        action = request.form.get("action")
        activity_id = request.form.get("id", type=int)
        name = norm_ws(request.form.get("name"))
        earns = request.form.get("earns") == "1"
        minutes_text = norm_ws(request.form.get("earned_minutes")) or "0"
        try:
            if action in ("add", "save"):
                if not name or not minutes_text.isdigit():
                    flash("Enter a name, and the earned minutes as a whole number.", "bad")
                elif action == "add":
                    top = db.one("select coalesce(max(sort), 0) + 1 as n from activity")["n"]
                    db.execute("insert into activity (name, token, earns, earned_minutes, sort) values (%s, %s, %s, %s, %s)",
                               (name, "a" + floor.new_token(), earns, int(minutes_text) if earns else 0, top))
                    flash(f"Added {name}. Reprint the posters to put its code on the wall.", "ok")
                elif activity_id:
                    db.execute("update activity set name = %s, earns = %s, earned_minutes = %s where id = %s",
                               (name, earns, int(minutes_text) if earns else 0, activity_id))
                    flash(f"{name} saved.", "ok")
            elif action == "toggle" and activity_id:
                db.execute("update activity set active = not active where id = %s", (activity_id,))
            elif action == "settings":
                change = norm_ws(request.form.get("changeover_minutes"))
                mostly = norm_ws(request.form.get("mostly_cut_pct"))
                day_end = norm_ws(request.form.get("day_end"))
                grace = norm_ws(request.form.get("auto_close_grace_hours"))
                if not (change.isdigit() and mostly.isdigit() and 1 <= int(mostly) <= 100 and grace.isdigit()
                        and re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", day_end)):
                    flash("Check the standards: minutes and hours are whole numbers, the warning is 1 to 100, "
                          "and the day end is a time like 17:00.", "bad")
                else:
                    for key, value in (("changeover_minutes", change), ("mostly_cut_pct", mostly),
                                       ("day_end", day_end), ("auto_close_grace_hours", grace)):
                        floor.set_setting(conn, key, value)
                    flash("Standards saved.", "ok")
            db.commit()
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash(f"There is already an activity called {name}.", "bad")
        return redirect(url_for("shop.activities"))
    rows = db.query("select a.*, (select count(*) from scan_session s where s.activity_id = a.id) as used "
                    "from activity a order by a.active desc, a.sort, a.id")
    machines = db.query("select * from machine where active order by sort, code")
    return render_template("shop/activities.html", rows=rows, settings=floor.settings(conn), machines=machines)


@bp.route("/posters/<int:machine_id>")
def poster(machine_id):
    machine = db.one("select * from machine where id = %s", (machine_id,))
    if not machine:
        return redirect(url_for("shop.activities"))
    acts = db.query("select * from activity where active order by sort, id")
    for a in acts:
        base = f"/s/a/{a['token']}/{machine['code']}"
        a["qr_on"] = qr.svg(qr.scan_url(base + "/on"), scale=4)
        a["qr_off"] = qr.svg(qr.scan_url(base + "/off"), scale=4)
    return render_template("shop/poster.html", machine=machine, acts=acts,
                           qr_all=qr.svg(qr.scan_url("/s/off-all"), scale=4),
                           qr_me=qr.svg(qr.scan_url("/s/me"), scale=4))


# ---- who is cutting now

@bp.route("/cutting")
def cutting():
    conn = db.get_db()
    now = datetime.now(PLANT_TZ)
    rows = db.query("""
        select s.*, tm.name as member_name, a.name as activity_name, a.earns, m.code as machine_code,
               m.name as machine_name,
               (select count(*) from sheet_tap t where t.session_id = s.id) as taps
          from scan_session s
          join team_member tm on tm.id = s.team_member_id
          left join activity a on a.id = s.activity_id
          left join machine m on m.id = s.machine_id
         where s.ended_at is null order by lower(tm.name), s.started_at, s.id
    """)
    people = {}
    for s in rows:
        s["minutes"] = (now - s["started_at"]).total_seconds() / 60.0
        s["co"] = floor.get_cut_order(conn, s["cut_order_id"]) if s["cut_order_id"] else None
        s["expected"] = None
        if s["co"] and not s["taps"]:
            std = floor.standard_seconds(conn, s["co"]["program_id"], s["machine_id"])
            if std:
                s["expected"] = int(s["minutes"] * 60 // std)
        people.setdefault(s["member_name"], []).append(s)
    for sessions in people.values():
        # Expected progress only means something when the person is on a single cut order.
        if sum(1 for s in sessions if s["co"]) != 1:
            for s in sessions:
                s["expected"] = None
    return render_template("shop/cutting.html", people=people, now=now)


# ---- daily minutes

def _day(arg):
    try:
        return datetime.strptime(arg or "", "%Y-%m-%d").date()
    except ValueError:
        return datetime.now(PLANT_TZ).date()


@bp.route("/minutes")
def minutes():
    day = _day(request.args.get("date"))
    people = floor.daily_minutes(db.get_db(), day)
    totals = {k: sum(p[k] for p in people) for k in ("clock", "earned", "earn_cut", "earn_change", "earn_activity", "sheets")}
    totals["ratio"] = 100.0 * totals["earned"] / totals["clock"] if totals["clock"] else None
    return render_template("shop/minutes.html", day=day, people=people, totals=totals,
                           prev=day - timedelta(days=1), next=day + timedelta(days=1),
                           today=datetime.now(PLANT_TZ).date())


@bp.route("/minutes.xlsx")
def minutes_xlsx():
    day = _day(request.args.get("date"))
    people = floor.daily_minutes(db.get_db(), day)
    wb = Workbook()
    ws = wb.active
    ws.title = "Daily minutes"
    ws.append(["Date", "Team member", "Clock minutes", "Earned minutes", "Earned %", "Cutting", "Changeovers",
               "Off order activities", "Sheets cut", "First scan", "Last scan", "Cut orders", "Activities",
               "Non-earning time", "No standard"])
    for p in people:
        ws.append([
            day, p["name"], round(p["clock"], 1), round(p["earned"], 1),
            round(p["ratio"], 1) if p["ratio"] is not None else None,
            round(p["earn_cut"], 1), round(p["earn_change"], 1), round(p["earn_activity"], 1), p["sheets"],
            p["first"].astimezone(PLANT_TZ).strftime("%-I:%M %p") if p["first"] else "",
            p["last"].astimezone(PLANT_TZ).strftime("%-I:%M %p") if p["last"] else "",
            ", ".join(p["orders"]), ", ".join(f"{k} x{v}" for k, v in p["activities"].items()),
            ", ".join(f"{k} {round(v)} min" for k, v in p["idle"].items()), ", ".join(p["no_standard"])])
    for col, width in zip("ABCDEFGHIJKLMNO", (12, 22, 14, 15, 10, 10, 13, 20, 11, 11, 11, 30, 30, 30, 24)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tpms-minutes-{day:%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- fix-up: scans the system had to close by itself

@bp.route("/fixup", methods=["GET", "POST"])
def fixup():
    conn = db.get_db()
    if request.method == "POST":
        session_id = request.form.get("id", type=int)
        s = db.one("select s.*, tm.name as member_name from scan_session s join team_member tm "
                   "on tm.id = s.team_member_id where s.id = %s and s.needs_review", (session_id,))
        if s:
            ended_text = norm_ws(request.form.get("ended_at"))
            sheets_text = norm_ws(request.form.get("sheets"))
            try:
                ended = datetime.strptime(ended_text, "%Y-%m-%dT%H:%M").replace(tzinfo=PLANT_TZ)
            except ValueError:
                ended = None
            if ended is None or ended < s["started_at"]:
                flash("The scan-off time has to be a time after the scan on.", "bad")
            elif s["cut_order_id"] and not re.fullmatch(r"[0-9]{1,4}", sheets_text):
                flash("Enter the sheets cut. Enter 0 if none.", "bad")
            else:
                sheets = int(sheets_text) if s["cut_order_id"] else None
                # The needs_review test makes a second save of the same row a no-op, so plywood is deducted once.
                saved = db.one("update scan_session set ended_at = %s, sheets = %s, needs_review = false, "
                               "closed_by = 'supervisor' where id = %s and needs_review returning id",
                               (ended, sheets, session_id))
                if saved and s["cut_order_id"]:
                    co = floor.get_cut_order(conn, s["cut_order_id"])
                    floor.post_issue(conn, co, session_id, sheets, s["member_name"], note="Entered on fix-up")
                    floor.refresh_status(conn, co["id"])
                db.commit()
                flash("Fixed.", "ok")
        return redirect(url_for("shop.fixup"))
    rows = db.query("""
        select s.*, tm.name as member_name, a.name as activity_name, m.code as machine_code,
               (select count(*) from sheet_tap t where t.session_id = s.id) as taps
          from scan_session s
          join team_member tm on tm.id = s.team_member_id
          left join activity a on a.id = s.activity_id
          left join machine m on m.id = s.machine_id
         where s.needs_review order by s.started_at
    """)
    for s in rows:
        s["co"] = floor.get_cut_order(conn, s["cut_order_id"]) if s["cut_order_id"] else None
        s["ended_local"] = s["ended_at"].astimezone(PLANT_TZ).strftime("%Y-%m-%dT%H:%M")
    return render_template("shop/fixup.html", rows=rows)
