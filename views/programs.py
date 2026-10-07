"""The CNC program list: search it, fix it, and load it from the legacy workbook."""
import io
import re

import psycopg
from flask import (Blueprint, Response, flash, redirect, render_template,
                   request, url_for)

import db
import floor
import importer
import nest
from guard import require_login, user_label
from util import norm_machine, norm_size, norm_thickness, norm_ws, parse_mmss
from views.items import all_items

bp = Blueprint("programs", __name__)
bp.before_request(require_login)

PAGE = 100
STATUSES = ["approved", "sample", "retired", "unknown"]
CO_LABELS = {"filed": "Filed", "queued": "Released", "complete": "Complete", "cancelled": "Cancelled"}


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
    has_nest = "exists (select 1 from program_nest n where n.program_id = p.id)"
    if args.get("nest") == "missing":
        where.append("not " + has_nest)
    elif args.get("nest") == "has":
        where.append(has_nest)
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
               exists (select 1 from program_nest n where n.program_id = p.id) as has_nest,
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
    nest_info, cuttings, item = None, [], None
    if program:
        nest_info = db.one("select filename, content_type, size_bytes, width, height, pages, uploaded_by, uploaded_at "
                           "from program_nest where program_id = %s", (program_id,))
        cuttings = [floor.decorate(r) for r in db.query(
            floor.CUT_ORDER_SQL + " where c.program_id = %s order by c.id desc limit 25", (program_id,))]
        if program["item_id"]:
            item = next((i for i in all_items() if i["id"] == program["item_id"]), None)
    return render_template("programs/form.html", program=program, form=form, errors=errors, machines=machines,
                           time_text=time_text, items=all_items(), statuses=STATUSES, nest=nest_info,
                           cuttings=cuttings, item=item, labels=CO_LABELS)


# ---- nest picture: one per program

@bp.route("/programs/<int:program_id>/nest", methods=["POST"])
def nest_upload(program_id):
    program = db.one("select id, number from program where id = %s", (program_id,))
    if not program:
        return redirect(url_for("programs.program_list"))
    back = redirect(url_for("programs.program_form", program_id=program_id) + "#nest")
    upload = request.files.get("nest")
    if not upload or not upload.filename:
        flash("Choose the nest picture or PDF first.", "bad")
        return back
    data = upload.read()
    try:
        info = nest.read_upload(data)
    except nest.NestError as exc:
        flash(str(exc), "bad")
        return back
    filename = norm_ws(upload.filename.replace("\\", "/").split("/")[-1])[:120] or "nest"
    db.execute("""
        insert into program_nest (program_id, filename, content_type, size_bytes, original, preview, preview_type,
                                  width, height, pages, uploaded_by)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (program_id) do update set
            filename = excluded.filename, content_type = excluded.content_type, size_bytes = excluded.size_bytes,
            original = excluded.original, preview = excluded.preview, preview_type = excluded.preview_type,
            width = excluded.width, height = excluded.height, pages = excluded.pages,
            uploaded_by = excluded.uploaded_by, uploaded_at = now()
    """, (program_id, filename, info["content_type"], len(data), data, info["preview"], info["preview_type"],
          info["width"], info["height"], info["pages"], user_label()))
    db.commit()
    note = " Only its first page is shown and printed." if info["pages"] > 1 else ""
    flash(f"Nest picture saved for {program['number']}.{note}", "ok")
    return back


@bp.route("/programs/<int:program_id>/nest/remove", methods=["POST"])
def nest_remove(program_id):
    db.execute("delete from program_nest where program_id = %s", (program_id,))
    db.commit()
    flash("Nest picture removed.", "ok")
    return redirect(url_for("programs.program_form", program_id=program_id) + "#nest")


@bp.route("/programs/<int:program_id>/nest/picture")
def nest_picture(program_id):
    """The nest as a picture: the upload itself, or page 1 of an uploaded PDF. ?tall=1 turns a wide one upright."""
    row = db.one("select preview, preview_type, uploaded_at from program_nest where program_id = %s", (program_id,))
    if not row:
        return ("No nest picture has been uploaded for this program.", 404)
    data = bytes(row["preview"])
    if request.args.get("tall") == "1":
        data = nest.upright(data, row["preview_type"])
    response = Response(data, mimetype=row["preview_type"])
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@bp.route("/programs/<int:program_id>/nest/file")
def nest_file(program_id):
    """The file exactly as it was uploaded."""
    row = db.one("select original, content_type, filename from program_nest where program_id = %s", (program_id,))
    if not row:
        return ("No nest picture has been uploaded for this program.", 404)
    response = Response(bytes(row["original"]), mimetype=row["content_type"])
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in row["filename"])
    response.headers["Content-Disposition"] = f'inline; filename="{safe}"'
    return response


@bp.route("/import", methods=["GET", "POST"])
def import_workbook():
    result = None
    if request.method == "POST":
        upload = request.files.get("workbook")
        want_programs = request.form.get("programs") == "1"
        want_items = request.form.get("items") == "1"
        want_jobs = request.form.get("jobs") == "1"
        if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("Choose the plywood workbook (.xlsx).", "bad")
        elif not (want_programs or want_items or want_jobs):
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
                result["items"] = importer.load_items(conn, importer.read_items(wb), entered_by=user_label())
            if want_programs:
                programs, report = importer.read_programs(wb)
                result["report"] = report
                result["programs"] = importer.load_programs(conn, programs,
                                                            overwrite=request.form.get("overwrite") == "1")
            if want_jobs:
                result["jobs"] = importer.load_jobs(conn, importer.read_jobs(wb), entered_by=user_label())
            db.commit()
    counts = db.one("select (select count(*) from program) as programs, (select count(*) from item) as items, "
                    "(select count(*) from cut_order where source = 'workbook') as jobs")
    return render_template("programs/import.html", result=result, counts=counts)
