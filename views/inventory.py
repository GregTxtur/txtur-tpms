"""Inventory: the material board, receiving, adjustments, and the ledger behind them all."""
import io
from datetime import datetime, timedelta

from flask import (Blueprint, flash, redirect, render_template, request,
                   send_file, url_for)
from openpyxl import Workbook

import db
from guard import require_login, user_label
from util import PLANT_TZ, item_label, item_sort_key, norm_ws
from views.items import ITEM_SQL, all_items

bp = Blueprint("inventory", __name__)
bp.before_request(require_login)

ADJUST_REASONS = ["Physical count", "Damaged sheets", "Bad sheet / scrap", "Found stock",
                  "Entry correction", "Other"]
TXN_LABELS = {"opening": "Opening", "receipt": "Receipt", "issue": "Cut", "adjust": "Adjustment",
              "return": "Return"}


def _post_txn(item_id, txn_type, qty, reason="", reference="", note=""):
    db.execute(
        "insert into inventory_txn (item_id, txn_type, qty_sheets, reason, reference, note, entered_by) "
        "values (%s, %s, %s, %s, %s, %s, %s)",
        (item_id, txn_type, qty, reason, reference, note, user_label()))


def _on_hand(item_id):
    return db.one("select coalesce(sum(qty_sheets), 0) as n from inventory_txn where item_id = %s", (item_id,))["n"]


def _whole_number(text):
    text = norm_ws(text).replace(",", "")
    return int(text) if text.lstrip("-").isdigit() else None


@bp.route("/board")
def board():
    rows = db.query("""
        select i.*, t.name as type_name, s.name as supplier_name,
               coalesce(x.on_hand, 0) as on_hand, x.last_receipt, x.last_count
          from item i
          join plywood_type t on t.id = i.type_id
          left join supplier s on s.id = i.supplier_id
          left join (select item_id, sum(qty_sheets) as on_hand,
                            max(created_at) filter (where txn_type = 'receipt') as last_receipt,
                            max(created_at) filter (where txn_type in ('adjust', 'opening')) as last_count
                       from inventory_txn group by item_id) x on x.item_id = i.id
         where i.active or coalesce(x.on_hand, 0) <> 0
    """)
    rows = sorted(rows, key=item_sort_key)
    total_value = 0
    for r in rows:
        r["packs"] = (r["on_hand"] / r["sheets_per_pack"]) if r["sheets_per_pack"] else None
        r["value"] = (r["on_hand"] * r["cost_per_sheet"]) if r["cost_per_sheet"] is not None else None
        if r["value"] and r["value"] > 0:
            total_value += r["value"]
        if r["on_hand"] < 0:
            r["flag"] = "negative"
        elif r["reorder_point"] is not None and r["on_hand"] <= r["reorder_point"]:
            r["flag"] = "reorder"
        else:
            r["flag"] = ""
    return render_template("inventory/board.html", rows=rows, total_value=total_value,
                           total_sheets=sum(r["on_hand"] for r in rows))


@bp.route("/receive", methods=["GET", "POST"])
def receive():
    items = all_items(active_only=True)
    form = {"item_id": request.args.get("item", type=int), "unit": "sheets"}
    if request.method == "POST":
        f = request.form
        form = {"item_id": f.get("item_id", type=int), "qty": f.get("qty", ""), "unit": f.get("unit", "sheets"),
                "reference": norm_ws(f.get("reference")), "note": norm_ws(f.get("note"))}
        item = next((i for i in items if i["id"] == form["item_id"]), None)
        qty = _whole_number(form["qty"])
        error = None
        if not item:
            error = "Pick the plywood that was received."
        elif qty is None or qty <= 0:
            error = "Enter how many were received, as a whole number."
        elif form["unit"] == "packs" and not item["sheets_per_pack"]:
            error = "That plywood has no sheets-per-pack set. Enter sheets, or set the pack size on the item first."
        if error:
            flash(error, "bad")
        else:
            sheets = qty * item["sheets_per_pack"] if form["unit"] == "packs" else qty
            _post_txn(item["id"], "receipt", sheets, reference=form["reference"], note=form["note"])
            db.commit()
            what = f"{qty} pack{'s' if qty != 1 else ''} ({sheets} sheets)" if form["unit"] == "packs" else f"{sheets} sheets"
            flash(f"Received {what} of {item_label(item)}. On hand now: {_on_hand(item['id']):,}.", "ok")
            return redirect(url_for("inventory.receive", item=item["id"]))
    recent = db.query("""
        select x.*, i.thickness, i.sheet_size, i.grade, t.name as type_name
          from inventory_txn x join item i on i.id = x.item_id join plywood_type t on t.id = i.type_id
         where x.txn_type = 'receipt' order by x.created_at desc limit 10
    """)
    return render_template("inventory/receive.html", items=items, form=form, recent=recent)


@bp.route("/adjust", methods=["GET", "POST"])
def adjust():
    items = all_items(active_only=True)
    form = {"item_id": request.args.get("item", type=int), "mode": "count", "reason": ADJUST_REASONS[0]}
    if request.method == "POST":
        f = request.form
        form = {"item_id": f.get("item_id", type=int), "mode": f.get("mode", "count"), "qty": f.get("qty", ""),
                "reason": f.get("reason", ""), "note": norm_ws(f.get("note"))}
        item = next((i for i in items if i["id"] == form["item_id"]), None)
        qty = _whole_number(form["qty"])
        error = None
        if not item:
            error = "Pick the plywood to adjust."
        elif qty is None:
            error = "Enter a whole number of sheets."
        elif form["mode"] == "count" and qty < 0:
            error = "A counted quantity cannot be negative."
        elif form["reason"] not in ADJUST_REASONS:
            error = "Pick a reason."
        elif form["reason"] == "Other" and not form["note"]:
            error = "Add a note saying why, when the reason is Other."
        if error:
            flash(error, "bad")
        else:
            before = _on_hand(item["id"])
            change = qty - before if form["mode"] == "count" else qty
            if change == 0:
                flash(f"No change: {item_label(item)} already shows {before:,} sheets.", "ok")
            else:
                note = form["note"]
                if form["mode"] == "count":
                    note = f"Counted {qty:,}; system showed {before:,}." + (f" {note}" if note else "")
                _post_txn(item["id"], "adjust", change, reason=form["reason"], note=note)
                db.commit()
                flash(f"Adjusted {item_label(item)} by {change:+,} sheets. On hand now: {before + change:,}.", "ok")
            return redirect(url_for("inventory.adjust", item=item["id"]))
    recent = db.query("""
        select x.*, i.thickness, i.sheet_size, i.grade, t.name as type_name
          from inventory_txn x join item i on i.id = x.item_id join plywood_type t on t.id = i.type_id
         where x.txn_type = 'adjust' order by x.created_at desc limit 10
    """)
    return render_template("inventory/adjust.html", items=items, form=form, reasons=ADJUST_REASONS, recent=recent)


def _ledger_rows(args, limit=None):
    where, params = [], []
    if args.get("item", type=int):
        where.append("x.item_id = %s")
        params.append(args.get("item", type=int))
    if args.get("type") in TXN_LABELS:
        where.append("x.txn_type = %s")
        params.append(args.get("type"))
    for field, column in (("so", "x.shop_order"), ("job", "x.job_number")):
        value = norm_ws(args.get(field))
        if value:
            where.append(f"{column} ilike %s")
            params.append(f"%{value}%")
    for field, op, shift in (("from", ">=", 0), ("to", "<", 1)):
        value = norm_ws(args.get(field))
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=shift)
            except ValueError:
                continue
            where.append(f"x.created_at {op} (%s::timestamp at time zone 'America/New_York')")
            params.append(day)
    sql = """
        select x.*, i.thickness, i.sheet_size, i.grade, i.code, t.name as type_name
          from inventory_txn x join item i on i.id = x.item_id join plywood_type t on t.id = i.type_id
    """ + (" where " + " and ".join(where) if where else "") + " order by x.created_at desc, x.id desc"
    if limit:
        sql += f" limit {int(limit)}"
    return db.query(sql, params)


@bp.route("/ledger")
def ledger():
    limit = 500
    rows = _ledger_rows(request.args, limit=limit + 1)
    return render_template("inventory/ledger.html", rows=rows[:limit], more=len(rows) > limit, limit=limit,
                           items=all_items(), labels=TXN_LABELS, args=request.args.to_dict(),
                           net=sum(r["qty_sheets"] for r in rows[:limit]))


@bp.route("/ledger.xlsx")
def ledger_xlsx():
    rows = _ledger_rows(request.args)
    wb = Workbook()
    ws = wb.active
    ws.title = "Ledger"
    ws.append(["Date", "Time", "Plywood", "Item code", "Type", "Sheets", "Reason", "Reference", "Job",
               "Shop order", "Team member", "Note", "Entered by"])
    for r in rows:
        local = r["created_at"].astimezone(PLANT_TZ)
        ws.append([local.date(), local.strftime("%I:%M %p"), item_label(r), r["code"], TXN_LABELS[r["txn_type"]],
                   r["qty_sheets"], r["reason"], r["reference"], r["job_number"], r["shop_order"],
                   r["team_member"], r["note"], r["entered_by"]])
    for col, width in zip("ABCDEFGHIJKLM", (12, 10, 30, 20, 12, 9, 20, 18, 12, 12, 16, 50, 14)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"tpms-ledger-{datetime.now():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
