"""Plywood setup: the item master, plus the type and supplier lists it draws on."""
import psycopg
from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
from auth import require_login
from util import item_sort_key, make_code, norm_size, norm_thickness, norm_ws

bp = Blueprint("items", __name__)
bp.before_request(require_login)

ITEM_SQL = """
    select i.*, t.name as type_name, s.name as supplier_name,
           coalesce((select sum(qty_sheets) from inventory_txn x where x.item_id = i.id), 0) as on_hand
      from item i
      join plywood_type t on t.id = i.type_id
      left join supplier s on s.id = i.supplier_id
"""


def all_items(active_only=False):
    rows = db.query(ITEM_SQL + (" where i.active" if active_only else ""))
    return sorted(rows, key=item_sort_key)


def _int_or_none(text, label, errors):
    text = norm_ws(text).replace(",", "")
    if not text:
        return None
    try:
        value = int(text)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return None


def _money_or_none(text, errors):
    text = norm_ws(text).replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        value = round(float(text), 2)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        errors.append("Cost per sheet must be a dollar amount.")
        return None


@bp.route("/items")
def item_list():
    show_all = request.args.get("all") == "1"
    return render_template("items/list.html", items=all_items(active_only=not show_all), show_all=show_all)


@bp.route("/items/new", methods=["GET", "POST"])
@bp.route("/items/<int:item_id>", methods=["GET", "POST"])
def item_form(item_id=None):
    item = db.one(ITEM_SQL + " where i.id = %s", (item_id,)) if item_id else None
    if item_id and not item:
        flash("That item no longer exists.", "bad")
        return redirect(url_for("items.item_list"))
    form = dict(item) if item else {"active": True}
    errors = []
    if request.method == "POST":
        f = request.form
        form = {
            "type_id": f.get("type_id", type=int),
            "thickness": norm_thickness(f.get("thickness")),
            "sheet_size": norm_size(f.get("sheet_size")),
            "grade": norm_ws(f.get("grade")),
            "supplier_id": f.get("supplier_id", type=int),
            "sheets_per_pack": _int_or_none(f.get("sheets_per_pack"), "Sheets per pack", errors),
            "cost_per_sheet": _money_or_none(f.get("cost_per_sheet"), errors),
            "lead_time_days": _int_or_none(f.get("lead_time_days"), "Lead time", errors),
            "reorder_point": _int_or_none(f.get("reorder_point"), "Reorder point", errors),
            "notes": norm_ws(f.get("notes")),
            "active": f.get("active") == "1",
            "code": norm_ws(f.get("code")),
        }
        type_row = db.one("select name from plywood_type where id = %s", (form["type_id"],)) if form["type_id"] else None
        if not type_row:
            errors.append("Pick a plywood type.")
        if form["supplier_id"] and not db.one("select 1 from supplier where id = %s", (form["supplier_id"],)):
            form["supplier_id"] = None
        if not form["thickness"]:
            errors.append("Thickness is required.")
        if not form["sheet_size"]:
            errors.append("Sheet size is required.")
        if form["sheets_per_pack"] == 0:
            errors.append("Sheets per pack must be more than zero, or left blank.")
        if not errors:
            if not form["code"]:
                form["code"] = make_code(form["sheet_size"], form["thickness"], type_row["name"], form["grade"])
            cols = ("code", "type_id", "thickness", "sheet_size", "grade", "supplier_id", "sheets_per_pack",
                    "cost_per_sheet", "lead_time_days", "reorder_point", "notes", "active")
            values = [form[c] for c in cols]
            try:
                if item:
                    db.execute("update item set " + ", ".join(f"{c} = %s" for c in cols) + " where id = %s",
                               values + [item_id])
                else:
                    db.execute(f"insert into item ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                               values)
                db.commit()
                flash("Item saved.", "ok")
                return redirect(url_for("items.item_list"))
            except psycopg.errors.UniqueViolation as exc:
                db.rollback()
                if "code" in str(exc):
                    errors.append("Another item already uses that code.")
                else:
                    errors.append("That plywood is already set up (same type, thickness, sheet size and grade).")
    return render_template(
        "items/form.html", item=item, form=form, errors=errors,
        types=db.query("select * from plywood_type where active or id = %s order by lower(name)",
                       (form.get("type_id") or 0,)),
        suppliers=db.query("select * from supplier where active or id = %s order by lower(name)",
                           (form.get("supplier_id") or 0,)),
        thicknesses=[r["thickness"] for r in db.query("select distinct thickness from item order by 1")],
        sizes=[r["sheet_size"] for r in db.query("select distinct sheet_size from item order by 1")],
    )


@bp.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    used = db.one("select (select count(*) from inventory_txn where item_id = %s) as txns, "
                  "(select count(*) from program where item_id = %s) as programs", (item_id, item_id))
    if used["txns"] or used["programs"]:
        flash("That plywood has history or programs linked to it, so it can be retired but not deleted.", "bad")
        return redirect(url_for("items.item_form", item_id=item_id))
    db.execute("delete from item where id = %s", (item_id,))
    db.commit()
    flash("Item deleted.", "ok")
    return redirect(url_for("items.item_list"))


# ---- types and suppliers: two short lists maintained the same way

LISTS = {
    "types": {"table": "plywood_type", "title": "Plywood types", "one": "type",
              "used": "select count(*) as n from item where type_id = %s"},
    "suppliers": {"table": "supplier", "title": "Suppliers", "one": "supplier",
                  "used": "select count(*) as n from item where supplier_id = %s"},
}


@bp.route("/setup/<kind>", methods=["GET", "POST"])
def setup_list(kind):
    if kind not in LISTS:
        return redirect(url_for("items.item_list"))
    spec = LISTS[kind]
    table = spec["table"]
    if request.method == "POST":
        action = request.form.get("action")
        name = norm_ws(request.form.get("name"))
        row_id = request.form.get("id", type=int)
        try:
            if action == "add" and name:
                db.execute(f"insert into {table} (name) values (%s)", (name,))
                flash(f"Added {name}.", "ok")
            elif action == "rename" and name and row_id:
                db.execute(f"update {table} set name = %s where id = %s", (name, row_id))
                flash(f"Renamed to {name}.", "ok")
            elif action == "toggle" and row_id:
                db.execute(f"update {table} set active = not active where id = %s", (row_id,))
            elif action == "delete" and row_id:
                if db.one(spec["used"], (row_id,))["n"]:
                    flash("That one is in use on an item, so it can be retired but not deleted.", "bad")
                else:
                    db.execute(f"delete from {table} where id = %s", (row_id,))
                    flash("Deleted.", "ok")
            elif action in ("add", "rename"):
                flash("Enter a name.", "bad")
            db.commit()
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash(f"There is already a {spec['one']} called {name}.", "bad")
        return redirect(url_for("items.setup_list", kind=kind))
    fk = "type_id" if kind == "types" else "supplier_id"
    rows = db.query(f"select l.*, (select count(*) from item i where i.{fk} = l.id) as used "
                    f"from {table} l order by lower(l.name)")
    return render_template("items/setup_list.html", kind=kind, spec=spec, rows=rows)
