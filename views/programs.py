"""The CNC program list: search it, fix it, and load it from the legacy workbook."""
import io
import re

import psycopg
from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
import importer
from auth import current_user, require_login
from util import norm_machine, norm_size, norm_thickness, norm_ws, parse_mmss
from views.items import all_items

bp = Blueprint("programs", __name__)
bp.before_request(require_login)

PAGE = 100
STATUSES = ["approved", "sample", "retired", "unknown"]


def _machines():
    return db.query("select * from machine order by sort, code")


def _filters(args):
    where, params = [], []
    q = norm_ws(args.get("q"))
    if q:
        where.append("(p.number ilike %s or p.name ilike %s or p.description ilike %s)")
        params += [f"%{q}%"] * 3
    for field, column in (("machine", "p.machine_code"), ("size", "p.sheet_size"), ("thick", "p.thickness"),
                          ("status", "p.status"), ("material", "p.material_text")):
        value = norm_ws(args.get(field))
        if value:
            where.append(f"{column} = %s")
            params.append(value)
    has_time = "exists (select 1 from program_time t where t.program_id = p.id)"
    if args.get("time") == "missing":
        where.append("not " + has_time)
    elif args.get("time") == "has":
        where.append(has_time)
    if args.get("item") == "linked":
        where.append("p.item_id is not null")
    elif args.get("item") == "unlinked":
        where.append("p.item_id is null")
    if args.get("check") == "dupes":
        where.append("p.number in (select number from program group by number having count(*) > 1)")
    elif args.get("check") == "odd":
        where.append("p.number_flag")
    return (" where " + " and ".join(where)) if where else "", params


@bp.route("/programs")
def program_list():
    where, params = _filters(request.args)
    page = max(request.args.get("page", 1, type=int), 1)
    total = db.one(f"select count(*) as n from program p{where}", params)["n"]
    rows = db.query(f"""
        select p.*, i.thickness as item_thickness, i.sheet_size as item_size, i.grade as item_grade,
               t.name as item_type,
               (select count(*) from program d where d.number = p.number) > 1 as dupe
          from program p
          left join item i on i.id = p.item_id
          left join plywood_type t on t.id = i.type_id
          {where}
         order by p.number, p.id
         limit {PAGE} offset {(page - 1) * PAGE}
    """, params)
    times = {}
    if rows:
        for t in db.query("select * from program_time where program_id = any(%s)", ([r["id"] for r in rows],)):
            times.setdefault(t["program_id"], {})[t["machine_id"]] = t["seconds_per_sheet"]
    args = request.args.to_dict()
    args.pop("page", None)
    choices = {
        "machine": [r["v"] for r in db.query("select distinct machine_code as v from program where machine_code <> '' order by 1")],
        "size": [r["v"] for r in db.query("select sheet_size as v from program where sheet_size <> '' group by 1 order by count(*) desc limit 12")],
        "thick": [r["v"] for r in db.query("select thickness as v from program where thickness <> '' group by 1 order by count(*) desc limit 20")],
        "material": [r["v"] for r in db.query("select material_text as v from program where material_text <> '' group by 1 order by count(*) desc limit 20")],
    }
    return render_template("programs/list.html", rows=rows, times=times, machines=_machines(), total=total,
                           page=page, pages=max((total + PAGE - 1) // PAGE, 1), args=args, choices=choices,
                           statuses=STATUSES)


def _num(text, label, errors, whole=False):
    text = norm_ws(text)
    if not text:
        return None
    try:
        value = float(text)
        if value <= 0:
            raise ValueError
        if whole and not value.is_integer():
            raise ValueError
        return int(value) if whole else value
    except ValueError:
        errors.append(f"{label} must be a {'whole ' if whole else ''}number above zero.")
        return None


@bp.route("/programs/new", methods=["GET", "POST"])
@bp.route("/programs/<int:program_id>", methods=["GET", "POST"])
def program_form(program_id=None):
    program = db.one("select * from program where id = %s", (program_id,)) if program_id else None
    if program_id and not program:
        flash("That program no longer exists.", "bad")
        return redirect(url_for("programs.program_list"))
    machines = _machines()
    form = dict(program) if program else {"status": "approved"}
    time_text = {}
    if program:
        for t in db.query("select * from program_time where program_id = %s", (program_id,)):
            secs = t["seconds_per_sheet"]
            time_text[t["machine_id"]] = f"{secs // 60}:{secs % 60:02d}"
    errors = []
    if request.method == "POST":
        f = request.form
        form = {
            "number": norm_ws(f.get("number")).upper(), "name": norm_ws(f.get("name")),
            "description": norm_ws(f.get("description")), "machine_code": norm_machine(f.get("machine_code")),
            "units_per_sheet": _num(f.get("units_per_sheet"), "Units per sheet", errors),
            "sheets_per_unit": _num(f.get("sheets_per_unit"), "Sheets per unit", errors),
            "parts_on_sheet": _num(f.get("parts_on_sheet"), "Parts on sheet", errors, whole=True),
            "parts_in_unit": _num(f.get("parts_in_unit"), "Parts in one unit", errors, whole=True),
            "sheet_size": norm_size(f.get("sheet_size")), "thickness": norm_thickness(f.get("thickness")),
            "material_text": norm_ws(f.get("material_text")), "item_id": f.get("item_id", type=int),
            "status": f.get("status") if f.get("status") in STATUSES else "unknown",
            "program_date": norm_ws(f.get("program_date")) or None, "notes": norm_ws(f.get("notes")),
        }
        form["number_flag"] = not re.fullmatch(r"P\d{4,5}", form["number"])
        new_times = {}
        for m in machines:
            time_text[m["id"]] = norm_ws(f.get(f"time_{m['id']}"))
            try:
                new_times[m["id"]] = parse_mmss(time_text[m["id"]])
            except ValueError:
                errors.append(f"{m['name']} time should look like 5:30 (minutes:seconds).")
        if not form["number"]:
            errors.append("Program number is required.")
        if form["item_id"] and not db.one("select 1 from item where id = %s", (form["item_id"],)):
            form["item_id"] = None
        if not errors:
            cols = list(form.keys())
            values = [form[c] for c in cols]
            try:
                if program:
                    db.execute("update program set " + ", ".join(f"{c} = %s" for c in cols)
                               + ", updated_at = now() where id = %s", values + [program_id])
                    pid = program_id
                else:
                    pid = db.one(f"insert into program ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
                                 "returning id", values)["id"]
                db.execute("delete from program_time where program_id = %s", (pid,))
                for machine_id, secs in new_times.items():
                    if secs:
                        db.execute("insert into program_time (program_id, machine_id, seconds_per_sheet) "
                                   "values (%s, %s, %s)", (pid, machine_id, secs))
                db.commit()
                flash(f"Program {form['number']} saved.", "ok")
                return redirect(request.args.get("back") if (request.args.get("back") or "").startswith("/programs")
                                else url_for("programs.program_list", q=form["number"]))
            except psycopg.errors.UniqueViolation:
                db.rollback()
                errors.append("There is already a program with that number and name.")
            except psycopg.errors.DataError:
                db.rollback()
                errors.append("One of the values could not be saved. Check the date (YYYY-MM-DD) and the numbers.")
    return render_template("programs/form.html", program=program, form=form, errors=errors, machines=machines,
                           time_text=time_text, items=all_items(), statuses=STATUSES)


@bp.route("/import", methods=["GET", "POST"])
def import_workbook():
    result = None
    if request.method == "POST":
        upload = request.files.get("workbook")
        want_programs = request.form.get("programs") == "1"
        want_items = request.form.get("items") == "1"
        if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("Choose the plywood workbook (.xlsx).", "bad")
        elif not (want_programs or want_items):
            flash("Tick at least one thing to load.", "bad")
        else:
            try:
                # Read into memory first: on Python 3.10 an upload stream is not seekable enough for zip files.
                wb = importer.open_workbook(io.BytesIO(upload.read()))
            except Exception:  # noqa: BLE001 - any unreadable file gets the same answer
                flash("That file could not be read as an Excel workbook.", "bad")
                return redirect(url_for("programs.import_workbook"))
            result = {"file": upload.filename}
            conn = db.get_db()
            if want_items:
                result["items"] = importer.load_items(conn, importer.read_items(wb), entered_by=current_user() or "")
            if want_programs:
                programs, report = importer.read_programs(wb)
                result["report"] = report
                result["programs"] = importer.load_programs(conn, programs,
                                                            overwrite=request.form.get("overwrite") == "1")
            db.commit()
    counts = db.one("select (select count(*) from program) as programs, (select count(*) from item) as items")
    return render_template("programs/import.html", result=result, counts=counts)
