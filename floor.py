"""Shop-floor rules: cut order progress, scan on / scan off, earned minutes.

Everything here takes a database connection and returns plain data, so the same rules
serve the phone pages, the supervisor screens and the tests.
"""
import math
import re
import secrets
from datetime import datetime, time, timedelta

from util import PLANT_TZ

DEFAULTS = {"changeover_minutes": "15", "mostly_cut_pct": "90", "day_end": "17:00",
            "auto_close_grace_hours": "4"}


# ---- settings

def settings(conn):
    out = dict(DEFAULTS)
    out.update({r["key"]: r["value"] for r in conn.execute("select key, value from setting")})
    return out


def setting_int(conn, key):
    try:
        return int(settings(conn)[key])
    except (KeyError, ValueError):
        return int(DEFAULTS[key])


def set_setting(conn, key, value):
    conn.execute("insert into setting (key, value) values (%s, %s) "
                 "on conflict (key) do update set value = excluded.value", (key, str(value)))


def day_end_time(conn):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", settings(conn)["day_end"].strip())
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return time(int(m.group(1)), int(m.group(2)))
    return time(17, 0)


# ---- cut orders

def new_token():
    return secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")


def co_number(cut_order_id):
    return f"CO-{int(cut_order_id):04d}"


def parse_co_number(text):
    """'CO-0042', 'co42' or '42' -> 42. None when it is not a cut order number."""
    m = re.fullmatch(r"\s*(?:CO)?[-\s]*0*(\d{1,9})\s*", text or "", re.I)
    return int(m.group(1)) if m else None


def units_per_sheet(program):
    """Order units one sheet of this program yields, or None when the program list does not say.

    The list's "units in 1 sheet" figure is per program and is used when present. Its "sheets to 1 unit"
    figure counts the sheets of a whole set (a "1 OF 3" program shows 3), so it is only used for a
    program that is not one part of a set.
    """
    if not program:
        return None
    if program.get("units_per_sheet"):
        return float(program["units_per_sheet"])
    name = program.get("program_name") or program.get("name") or ""
    if program.get("sheets_per_unit") and not re.search(r"\d\s*OF\s*\d", name, re.I):
        return 1.0 / float(program["sheets_per_unit"])
    return None


def sheets_for(qty_units, program):
    """Sheets needed for an order quantity, from the program's yield. None when the program does not say."""
    per_sheet = units_per_sheet(program)
    if not qty_units or not per_sheet:
        return None
    return max(1, math.ceil(float(qty_units) / per_sheet - 1e-9))


def units_for(sheets, program):
    """Order units a number of sheets yields, or None when the program's yield is unknown."""
    per_sheet = units_per_sheet(program)
    if sheets is None or not per_sheet:
        return None
    return float(sheets) * per_sheet


CUT_ORDER_SQL = """
    select c.*, p.number as program_number, p.name as program_name, p.units_per_sheet, p.sheets_per_unit,
           p.sheet_size as program_size, p.thickness as program_thickness,
           i.sheet_size, i.thickness, i.grade, t.name as type_name,
           m.code as machine_code, m.name as machine_name,
           exists (select 1 from program_nest n where n.program_id = c.program_id) as has_nest,
           coalesce((select sum(s.sheets) from scan_session s where s.cut_order_id = c.id), 0)::int as sheets_cut,
           (select count(*) from scan_session s where s.cut_order_id = c.id and s.ended_at is null) as on_now
      from cut_order c
      left join program p on p.id = c.program_id
      left join item i on i.id = c.item_id
      left join plywood_type t on t.id = i.type_id
      left join machine m on m.id = c.machine_id
"""


def decorate(co):
    """Add the derived numbers every screen shows: number, open sheets, units cut, share cut."""
    co["number"] = co_number(co["id"])
    req = co["sheets_required"]
    co["sheets_open"] = max(req - co["sheets_cut"], 0) if req is not None else None
    co["pct_cut"] = (100.0 * co["sheets_cut"] / req) if req else None
    co["units_cut"] = units_for(co["sheets_cut"], co)
    co["yield_per_sheet"] = units_per_sheet(co)
    co["plywood"] = (f"{co['sheet_size']} {co['thickness']} {co['type_name']}"
                     + (f" · {co['grade']}" if co.get("grade") else "")) if co.get("item_id") else ""
    return co


def get_cut_order(conn, cut_order_id=None, token=None):
    if token is not None:
        row = conn.execute(CUT_ORDER_SQL + " where c.token = %s", (token,)).fetchone()
    else:
        row = conn.execute(CUT_ORDER_SQL + " where c.id = %s", (cut_order_id,)).fetchone()
    return decorate(row) if row else None


def refresh_status(conn, cut_order_id):
    """A queued cut order becomes complete once the sheets cut reach the sheets required."""
    co = get_cut_order(conn, cut_order_id)
    if co and co["status"] == "queued" and co["sheets_required"] and co["sheets_cut"] >= co["sheets_required"]:
        conn.execute("update cut_order set status = 'complete', completed_at = now() where id = %s", (cut_order_id,))
        return "complete"
    return co["status"] if co else None


def standard_seconds(conn, program_id, machine_id):
    """Standard router seconds per sheet: this router's time, else any time the program has. None if none."""
    if not program_id:
        return None
    rows = conn.execute(
        "select t.machine_id, t.seconds_per_sheet from program_time t join machine m on m.id = t.machine_id "
        "where t.program_id = %s order by m.sort, m.id", (program_id,)).fetchall()
    if not rows:
        return None
    for r in rows:
        if machine_id and r["machine_id"] == machine_id:
            return r["seconds_per_sheet"]
    return rows[0]["seconds_per_sheet"]


# ---- scanning

def open_sessions(conn, team_member_id):
    rows = conn.execute("""
        select s.*, a.name as activity_name, a.earns, m.code as machine_code, m.name as machine_name,
               (select count(*) from sheet_tap t where t.session_id = s.id) as taps
          from scan_session s
          left join activity a on a.id = s.activity_id
          left join machine m on m.id = s.machine_id
         where s.team_member_id = %s and s.ended_at is null
         order by s.started_at, s.id
    """, (team_member_id,)).fetchall()
    for r in rows:
        r["cut_order"] = get_cut_order(conn, r["cut_order_id"]) if r["cut_order_id"] else None
    return rows


def changeover_credit(conn, co):
    """Minutes earned for setting up this cut order's program.

    Earned once per cut order (on its first scan on, by whoever that is), and not at all when the
    cut order last started on the same router ran the same program. A cut order with no program
    earns none: there is no program to change to.
    """
    if not co["program_id"]:
        return 0
    if conn.execute("select 1 from scan_session where cut_order_id = %s limit 1", (co["id"],)).fetchone():
        return 0
    if co["machine_id"]:
        prev = conn.execute("""
            select c.program_id from scan_session s join cut_order c on c.id = s.cut_order_id
             where s.machine_id = %s and s.cut_order_id <> %s
             order by s.started_at desc, s.id desc limit 1
        """, (co["machine_id"], co["id"])).fetchone()
        if prev and prev["program_id"] == co["program_id"]:
            return 0
    return setting_int(conn, "changeover_minutes")


def scan_on_cut_order(conn, team_member_id, co):
    """Start a session. Returns (session_id, already_on)."""
    conn.execute("select 1 from cut_order where id = %s for update", (co["id"],))
    existing = conn.execute(
        "select id from scan_session where team_member_id = %s and cut_order_id = %s and ended_at is null",
        (team_member_id, co["id"])).fetchone()
    if existing:
        return existing["id"], True
    credit = changeover_credit(conn, co)
    row = conn.execute(
        "insert into scan_session (team_member_id, cut_order_id, machine_id, fixed_minutes) "
        "values (%s, %s, %s, %s) on conflict do nothing returning id",
        (team_member_id, co["id"], co["machine_id"], credit)).fetchone()
    if row is None:  # a second tap landed first
        row = conn.execute("select id from scan_session where team_member_id = %s and cut_order_id = %s "
                           "and ended_at is null", (team_member_id, co["id"])).fetchone()
        return row["id"], True
    return row["id"], False


def scan_off_cut_order(conn, session_id, sheets, member_name, closed_by="self", ended_at=None):
    """Close a cut-order session with its sheet count, deduct the plywood, and update the order.

    Returns the cut order afterwards, or None if the session was not open (for example a double tap).
    """
    peek = conn.execute("select cut_order_id from scan_session where id = %s", (session_id,)).fetchone()
    if not peek or not peek["cut_order_id"]:
        return None
    conn.execute("select 1 from cut_order where id = %s for update", (peek["cut_order_id"],))
    s = conn.execute("select * from scan_session where id = %s for update", (session_id,)).fetchone()
    if not s or s["ended_at"] is not None:
        return None
    conn.execute(
        "update scan_session set ended_at = coalesce(%s, now()), sheets = %s, closed_by = %s, needs_review = false "
        "where id = %s", (ended_at, sheets, closed_by, session_id))
    co = get_cut_order(conn, s["cut_order_id"])
    post_issue(conn, co, session_id, sheets, member_name)
    refresh_status(conn, co["id"])
    return get_cut_order(conn, co["id"])


def post_issue(conn, co, session_id, sheets, member_name, reason="Cut", note=""):
    """Deduct sheets for a cut order (negative ledger entry). A negative `sheets` gives sheets back."""
    if not sheets or not co.get("item_id"):
        return
    conn.execute(
        "insert into inventory_txn (item_id, txn_type, qty_sheets, reason, reference, job_number, shop_order, "
        "team_member, note, entered_by, scan_session_id) values (%s, 'issue', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (co["item_id"], -int(sheets), reason, co["number"], co["job_number"], co["shop_order"], member_name,
         note, member_name, session_id))


def scan_on_activity(conn, team_member_id, activity, machine_id):
    existing = conn.execute(
        "select id from scan_session where team_member_id = %s and activity_id = %s "
        "and coalesce(machine_id, 0) = %s and ended_at is null",
        (team_member_id, activity["id"], machine_id or 0)).fetchone()
    if existing:
        return existing["id"], True
    fixed = activity["earned_minutes"] if activity["earns"] else 0
    row = conn.execute(
        "insert into scan_session (team_member_id, activity_id, machine_id, fixed_minutes) "
        "values (%s, %s, %s, %s) on conflict do nothing returning id",
        (team_member_id, activity["id"], machine_id, fixed)).fetchone()
    if row is None:
        row = conn.execute(
            "select id from scan_session where team_member_id = %s and activity_id = %s "
            "and coalesce(machine_id, 0) = %s and ended_at is null",
            (team_member_id, activity["id"], machine_id or 0)).fetchone()
        return row["id"], True
    return row["id"], False


def scan_off_activity(conn, team_member_id, activity_id, machine_id):
    row = conn.execute(
        "update scan_session set ended_at = now(), closed_by = 'self' where team_member_id = %s "
        "and activity_id = %s and coalesce(machine_id, 0) = %s and ended_at is null returning id, started_at, ended_at",
        (team_member_id, activity_id, machine_id or 0)).fetchone()
    return row


def close_stale(conn, now=None):
    """Close scans that were plainly forgotten and flag them for the supervisor.

    A scan is stale once the clock passes day_end + grace on the day it started. It is closed back to
    day_end (or to its own start, if it started after day_end), with no sheet count: the supervisor
    supplies that on the fix-up screen, and only then is plywood deducted.
    """
    now = now or datetime.now(PLANT_TZ)
    end_t = day_end_time(conn)
    grace = timedelta(hours=setting_int(conn, "auto_close_grace_hours"))
    closed = 0
    for s in conn.execute("select id, started_at from scan_session where ended_at is null").fetchall():
        started = s["started_at"].astimezone(PLANT_TZ)
        day_end = datetime.combine(started.date(), end_t, PLANT_TZ)
        if now > max(day_end, started) + grace:
            conn.execute(
                "update scan_session set ended_at = %s, closed_by = 'auto', needs_review = true where id = %s "
                "and ended_at is null", (max(day_end, started), s["id"]))
            closed += 1
    return closed


# ---- minutes

def _merge(intervals):
    """Total seconds covered by a set of (start, end) intervals, counting overlaps once."""
    total, cur_start, cur_end = 0.0, None, None
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += (cur_end - cur_start).total_seconds()
    return total


def daily_minutes(conn, day, now=None):
    """Per team member for one plant-time day: clock minutes, earned minutes and what they came from.

    Clock   = time scanned onto anything, each minute counted once however many scans overlap.
    Earned  = sheets cut x the program's standard time (credited the day the count was entered)
            + changeover credits and off-order-activity standards (credited the day the scan started).
    """
    now = now or datetime.now(PLANT_TZ)
    start = datetime.combine(day, time(0, 0), PLANT_TZ)
    end = start + timedelta(days=1)
    rows = conn.execute("""
        select s.*, tm.name as member_name, c.program_id, c.description as co_description,
               a.name as activity_name, a.earns, m.code as machine_code
          from scan_session s
          join team_member tm on tm.id = s.team_member_id
          left join cut_order c on c.id = s.cut_order_id
          left join activity a on a.id = s.activity_id
          left join machine m on m.id = s.machine_id
         where s.started_at < %s and coalesce(s.ended_at, %s) >= %s
         order by tm.name, s.started_at, s.id
    """, (end, now, start)).fetchall()
    std_cache = {}
    people = {}
    for s in rows:
        p = people.setdefault(s["team_member_id"], {
            "id": s["team_member_id"], "name": s["member_name"], "intervals": [], "earn_cut": 0.0,
            "earn_change": 0.0, "earn_activity": 0.0, "sheets": 0, "orders": [], "activities": {},
            "idle": {}, "no_standard": [], "first": None, "last": None, "open_now": False, "needs_review": 0})
        s_end = s["ended_at"] or now
        lo, hi = max(s["started_at"], start), min(s_end, end)
        if hi > lo:
            p["intervals"].append((lo, hi))
            p["first"] = lo if p["first"] is None else min(p["first"], lo)
            p["last"] = hi if p["last"] is None else max(p["last"], hi)
        if s["ended_at"] is None:
            p["open_now"] = True
        if s["needs_review"]:
            p["needs_review"] += 1
        started_today = start <= s["started_at"] < end
        if s["cut_order_id"]:
            number = co_number(s["cut_order_id"])
            if number not in p["orders"]:
                p["orders"].append(number)
            if started_today:
                p["earn_change"] += s["fixed_minutes"]
            if s["sheets"] and s["ended_at"] is not None and start <= s["ended_at"] < end:
                p["sheets"] += s["sheets"]
                key = (s["program_id"], s["machine_id"])
                if key not in std_cache:
                    std_cache[key] = standard_seconds(conn, s["program_id"], s["machine_id"])
                if std_cache[key]:
                    p["earn_cut"] += s["sheets"] * std_cache[key] / 60.0
                elif number not in p["no_standard"]:
                    p["no_standard"].append(number)
        else:
            name = s["activity_name"]
            if started_today:
                p["activities"][name] = p["activities"].get(name, 0) + 1
                p["earn_activity"] += s["fixed_minutes"]
            if not s["earns"] and hi > lo:
                p["idle"][name] = p["idle"].get(name, 0.0) + (hi - lo).total_seconds() / 60.0
    out = []
    for p in people.values():
        p["clock"] = _merge(p.pop("intervals")) / 60.0
        p["earned"] = p["earn_cut"] + p["earn_change"] + p["earn_activity"]
        p["ratio"] = (100.0 * p["earned"] / p["clock"]) if p["clock"] > 0 else None
        out.append(p)
    return sorted(out, key=lambda p: p["name"].lower())
