"""Cut orders: the supervisor's short form, the list, release, and the printed cover sheet."""
from datetime import datetime

import psycopg
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

import db
import floor
import qr
from guard import require_login, user_label
from util import fmt_secs, norm_ws
from views.items import all_items

bp = Blueprint("cutorders", __name__)
bp.before_request(require_login)


@bp.before_request
def _tidy():
    """Close plainly forgotten scans first, so these pages never show yesterday's person as still on."""
    if floor.close_stale(db.get_db()):
        db.commit()

STATUS_LABELS = {"filed": "Filed", "queued": "Released", "complete": "Complete", "cancelled": "Cancelled"}
VIEWS = {"open": ("filed", "queued"), "filed": ("filed",), "queued": ("queued",), "complete": ("complete",),
         "all": ("filed", "queued", "complete", "cancelled")}


def _machines():
    return db.query("select * from machine where active order by sort, code")


def _programs_numbered(number):
    return db.query("""
        select p.*, i.sheet_size as item_size, i.thickness as item_thickness, i.grade as item_grade,
               t.name as item_type
          from program p
          left join item i on i.id = p.item_id
          left join plywood_type t on t.id = i.type_id
         where upper(p.number) = %s order by p.id
    """, (norm_ws(number).upper(),))


@bp.route("/cut-orders")
def cut_order_list():
    view = request.args.get("view", "open")
    statuses = VIEWS.get(view, VIEWS["open"])
    q = norm_ws(request.args.get("q"))
    where, params = ["c.status = any(%s)"], [list(statuses)]
    if q:
        number = floor.parse_co_number(q) if q.upper().startswith("CO") else None
        if number:
            where.append("c.id = %s")
            params.append(number)
        else:
            where.append("(c.shop_order ilike %s or c.job_number ilike %s or c.description ilike %s "
                         "or p.number ilike %s)")
            params += [f"%{q}%"] * 4
    rows = db.query(
        "select x.*, (select string_agg(tm.name, ', ' order by tm.name) from scan_session s "
        "join team_member tm on tm.id = s.team_member_id where s.cut_order_id = x.id and s.ended_at is null) "
        "as who_on from (" + floor.CUT_ORDER_SQL + " where " + " and ".join(where) + ") x "
        "order by case x.status when 'queued' then 0 when 'filed' then 1 else 2 end, "
        "x.due_date nulls last, x.id limit 500", params)
    rows = [floor.decorate(r) for r in rows]
    counts = {r["status"]: r["n"] for r in db.query("select status, count(*) as n from cut_order group by status")}
    return render_template("cutorders/list.html", rows=rows, view=view, q=q, counts=counts, labels=STATUS_LABELS)


@bp.route("/api/program")
def api_program():
    """What the cut order form shows as the supervisor types a program number."""
    out = []
    for p in _programs_numbered(request.args.get("number", "")):
        times = db.query("select m.code, t.seconds_per_sheet from program_time t join machine m "
                         "on m.id = t.machine_id where t.program_id = %s order by m.sort", (p["id"],))
        out.append({
            "id": p["id"], "number": p["number"], "name": p["name"], "description": p["description"],
            "size": p["sheet_size"], "thickness": p["thickness"], "material": p["material_text"],
            "units_per_sheet": float(p["units_per_sheet"]) if p["units_per_sheet"] is not None else None,
            "sheets_per_unit": float(p["sheets_per_unit"]) if p["sheets_per_unit"] is not None else None,
            "item_id": p["item_id"],
            "item": (f"{p['item_size']} {p['item_thickness']} {p['item_type']}" if p["item_id"] else ""),
            "times": ", ".join(f"{t['code']} {fmt_secs(t['seconds_per_sheet'])}" for t in times),
        })
    return jsonify(out)


def _positive(text, label, errors, whole=False):
    text = norm_ws(text).replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
        if value <= 0 or (whole and not value.is_integer()):
            raise ValueError
        return int(value) if whole else value
    except ValueError:
        errors.append(f"{label} must be a {'whole ' if whole else ''}number above zero.")
        return None


@bp.route("/cut-orders/new", methods=["GET", "POST"])
@bp.route("/cut-orders/<int:cut_order_id>/edit", methods=["GET", "POST"])
def cut_order_form(cut_order_id=None):
    co = floor.get_cut_order(db.get_db(), cut_order_id) if cut_order_id else None
    if cut_order_id and not co:
        flash("That cut order no longer exists.", "bad")
        return redirect(url_for("cutorders.cut_order_list"))
    if co and co["status"] in ("complete", "cancelled"):
        flash(f"{co['number']} is {STATUS_LABELS[co['status']].lower()} and can no longer be edited.", "bad")
        return redirect(url_for("cutorders.cut_order_detail", cut_order_id=co["id"]))
    form, choices, errors = {}, [], []
    if co:
        form = {k: co[k] for k in ("shop_order", "job_number", "description", "program_id", "item_id",
                                   "machine_id", "qty_units", "sheets_required", "due_date", "notes")}
        form["program_number"] = co["program_number"] or ""
    elif request.args.get("copy", type=int):
        src = floor.get_cut_order(db.get_db(), request.args.get("copy", type=int))
        if src:
            # Sheets required is left out on purpose: it is worked out again from the new quantity.
            form = {k: src[k] for k in ("description", "program_id", "item_id", "machine_id", "qty_units", "notes")}
            form["program_number"] = src["program_number"] or ""
    if request.method == "POST":
        f = request.form
        release = f.get("action") == "release"
        form = {
            "program_number": norm_ws(f.get("program_number")).upper(),
            "program_id": f.get("program_id", type=int),
            "qty_units": _positive(f.get("qty_units"), "Quantity", errors),
            "sheets_required": _positive(f.get("sheets_required"), "Sheets required", errors, whole=True),
            "shop_order": norm_ws(f.get("shop_order")), "job_number": norm_ws(f.get("job_number")),
            "description": norm_ws(f.get("description")),
            "machine_id": f.get("machine_id", type=int), "item_id": f.get("item_id", type=int),
            "due_date": norm_ws(f.get("due_date")) or None, "notes": norm_ws(f.get("notes")),
        }
        program = None
        if form["program_number"]:
            matches = _programs_numbered(form["program_number"])
            picked = [p for p in matches if p["id"] == form["program_id"]]
            if not matches:
                errors.append(f"There is no program numbered {form['program_number']}. Check the number, "
                              "or leave it blank to file the job without a program.")
            elif picked:
                program = picked[0]
            elif len(matches) == 1:
                program = matches[0]
            else:
                choices = matches
                errors.append(f"{len(matches)} programs share the number {form['program_number']}. Pick the one you mean.")
        form["program_id"] = program["id"] if program else None
        if program and not form["item_id"] and program["item_id"]:
            form["item_id"] = program["item_id"]
        if not form["description"] and program:
            form["description"] = norm_ws(f"{program['name']} {program['description']}")
        worked_out = floor.sheets_for(form["qty_units"], program)
        if form["sheets_required"] is None:
            form["sheets_required"] = worked_out
        elif co and worked_out and form["sheets_required"] == co["sheets_required"] and (
                form["qty_units"] != (float(co["qty_units"]) if co["qty_units"] is not None else None)
                or form["program_id"] != co["program_id"]):
            # The quantity or program changed but the sheets box still holds the old figure: follow the change.
            form["sheets_required"] = worked_out
        if not form["qty_units"] and not form["sheets_required"] and not errors:
            errors.append("Enter the quantity, or the number of sheets.")
        if form["due_date"]:
            try:
                datetime.strptime(form["due_date"], "%Y-%m-%d")
            except ValueError:
                errors.append("The due date is not a date.")
        if (release or (co and co["status"] == "queued")) and not errors:
            if not form["sheets_required"]:
                errors.append("A released cut order needs the sheets required: this program does not say how many "
                              "units come off a sheet, so enter the sheets yourself.")
            if not form["item_id"]:
                errors.append("A released cut order needs its plywood, so the stock can be deducted. Pick it below.")
            if not form["machine_id"]:
                errors.append("A released cut order needs its router.")
        if not errors:
            cols = ["shop_order", "job_number", "description", "program_id", "item_id", "machine_id",
                    "qty_units", "sheets_required", "due_date", "notes"]
            values = [form[c] for c in cols]
            try:
                if co:
                    db.execute("update cut_order set " + ", ".join(f"{c} = %s" for c in cols) + " where id = %s",
                               values + [co["id"]])
                    new_id = co["id"]
                else:
                    new_id = db.one(
                        f"insert into cut_order ({', '.join(cols)}, token, created_by) values "
                        f"({', '.join(['%s'] * len(cols))}, %s, %s) returning id",
                        values + [floor.new_token(), user_label()])["id"]
                # Remember which plywood this program cuts from, so nobody is asked twice.
                if program and form["item_id"] and not program["item_id"]:
                    db.execute("update program set item_id = %s, updated_at = now() where id = %s and item_id is null",
                               (form["item_id"], program["id"]))
                if release and (not co or co["status"] == "filed"):
                    db.execute("update cut_order set status = 'queued', released_at = now(), released_by = %s "
                               "where id = %s", (user_label(), new_id))
                db.commit()
            except psycopg.errors.DataError:
                db.rollback()
                errors.append("One of the values could not be saved. Check the numbers and the date.")
            else:
                if release:
                    return redirect(url_for("cutorders.cut_order_print", cut_order_id=new_id))
                flash(f"{floor.co_number(new_id)} saved.", "ok")
                return redirect(url_for("cutorders.cut_order_detail", cut_order_id=new_id))
    if form.get("program_number") and not choices:
        matches = _programs_numbered(form["program_number"])
        if len(matches) > 1:
            choices = matches
    return render_template("cutorders/form.html", co=co, form=form, errors=errors, choices=choices,
                           machines=_machines(), items=all_items(active_only=True))


@bp.route("/cut-orders/<int:cut_order_id>")
def cut_order_detail(cut_order_id):
    co = floor.get_cut_order(db.get_db(), cut_order_id)
    if not co:
        flash("That cut order no longer exists.", "bad")
        return redirect(url_for("cutorders.cut_order_list"))
    sessions = db.query("""
        select s.*, tm.name as member_name, m.code as machine_code,
               (select count(*) from sheet_tap t where t.session_id = s.id) as taps
          from scan_session s join team_member tm on tm.id = s.team_member_id
          left join machine m on m.id = s.machine_id
         where s.cut_order_id = %s order by s.started_at desc, s.id desc
    """, (cut_order_id,))
    std = floor.standard_seconds(db.get_db(), co["program_id"], co["machine_id"])
    return render_template("cutorders/detail.html", co=co, sessions=sessions, labels=STATUS_LABELS, std=std,
                           machines=_machines())


@bp.route("/cut-orders/<int:cut_order_id>/status", methods=["POST"])
def cut_order_status(cut_order_id):
    co = floor.get_cut_order(db.get_db(), cut_order_id)
    if not co:
        return redirect(url_for("cutorders.cut_order_list"))
    action = request.form.get("action")
    back = redirect(url_for("cutorders.cut_order_detail", cut_order_id=cut_order_id))
    if action == "release" and co["status"] == "filed":
        machine_id = request.form.get("machine_id", type=int) or co["machine_id"]
        missing = [label for label, value in (("the router", machine_id), ("the plywood", co["item_id"]),
                                              ("the sheets required", co["sheets_required"])) if not value]
        if missing:
            flash("Before releasing, set " + ", ".join(missing) + ". Use Edit.", "bad")
            return back
        db.execute("update cut_order set status = 'queued', machine_id = %s, released_at = now(), "
                   "released_by = %s where id = %s", (machine_id, user_label(), cut_order_id))
        db.commit()
        return redirect(url_for("cutorders.cut_order_print", cut_order_id=cut_order_id))
    if action == "unrelease" and co["status"] == "queued":
        if db.one("select 1 from scan_session where cut_order_id = %s limit 1", (cut_order_id,)):
            flash("This one has been scanned, so it cannot go back to Filed.", "bad")
            return back
        db.execute("update cut_order set status = 'filed', released_at = null, released_by = '' where id = %s",
                   (cut_order_id,))
    elif action == "complete" and co["status"] == "queued":
        if co["on_now"]:
            flash("Someone is still scanned onto this cut order. Have them scan off first.", "bad")
            return back
        db.execute("update cut_order set status = 'complete', completed_at = now() where id = %s", (cut_order_id,))
    elif action == "reopen" and co["status"] in ("complete", "cancelled"):
        db.execute("update cut_order set status = %s, completed_at = null where id = %s",
                   ("queued" if co["released_at"] else "filed", cut_order_id))
    elif action == "cancel" and co["status"] in ("filed", "queued"):
        if co["on_now"]:
            flash("Someone is still scanned onto this cut order. Have them scan off first.", "bad")
            return back
        db.execute("update cut_order set status = 'cancelled' where id = %s", (cut_order_id,))
    db.commit()
    return back


@bp.route("/cut-orders/<int:cut_order_id>/print")
def cut_order_print(cut_order_id):
    co = floor.get_cut_order(db.get_db(), cut_order_id)
    if not co:
        flash("That cut order no longer exists.", "bad")
        return redirect(url_for("cutorders.cut_order_list"))
    if co["status"] == "filed":
        flash("Release the cut order first. The cover sheet prints on release.", "bad")
        return redirect(url_for("cutorders.cut_order_detail", cut_order_id=cut_order_id))
    std = floor.standard_seconds(db.get_db(), co["program_id"], co["machine_id"])
    return render_template(
        "cutorders/print.html", co=co, std=std,
        qr_on=qr.svg(qr.scan_url(f"/s/c/{co['token']}/on"), scale=7),
        qr_off=qr.svg(qr.scan_url(f"/s/c/{co['token']}/off"), scale=7),
        short_url=qr.public_base().split("//")[-1] + "/s/me")
