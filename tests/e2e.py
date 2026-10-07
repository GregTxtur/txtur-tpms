"""End-to-end run of TPMS against a fresh, throwaway database. Exits non-zero on the first failed check.

Never point this at the live database: it creates and deletes data freely.

    createdb tpms_test                      # empty database the app can migrate
    DATABASE_URL=postgresql://.../tpms_test venv/bin/gunicorn --bind 127.0.0.1:8003 app:app &
    TPMS_TEST_DB=postgresql://.../tpms_test TPMS_TEST_WORKBOOK=/path/to/workbook.xlsx venv/bin/python tests/e2e.py

The workbook is the legacy "Active Orders on Plywood" file; it is business data and is not kept in this repository.
The app under test must run with the Microsoft-gate auth module (office requests carry X-Auth-Request-Email).
Needs the `requests` package in addition to requirements.txt.
"""
import math, os, re, sys
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
import psycopg, requests
from psycopg.rows import dict_row

B = os.environ.get("TPMS_TEST_URL", "http://127.0.0.1:8003")
WB = os.environ["TPMS_TEST_WORKBOOK"]
LOG = os.environ.get("TPMS_TEST_LOG", "")
TZ = ZoneInfo("America/New_York")
db = psycopg.connect(os.environ["TPMS_TEST_DB"], row_factory=dict_row, autocommit=True)
office = requests.Session(); office.headers["X-Auth-Request-Email"] = "greg@txtur.com"
checks = 0

def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", what); sys.exit(1)

def one(sql, *a): return db.execute(sql, a).fetchone()
def text(r): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))

# 1. load the workbook: programs, items, open jobs
r = office.post(B + "/import", files={"workbook": open(WB, "rb")}, data={"programs": 1, "items": 1, "jobs": 1})
ok(r.status_code == 200 and "programs added" in r.text, "import")
ok(one("select count(*) n from program")["n"] == 5082, "5082 programs")
jobs = one("select count(*) n, count(*) filter (where status='queued') q, count(*) filter (where status='filed') f, count(*) filter (where item_id is null) noitem from cut_order")
print("workbook jobs:", jobs)
ok(jobs["n"] > 100 and jobs["q"] > 15 and jobs["f"] > 80, "jobs loaded from both sheets")
r = office.post(B + "/import", files={"workbook": open(WB, "rb")}, data={"jobs": 1})
ok(one("select count(*) n from cut_order")["n"] == jobs["n"], "job import is repeatable")

# 2. every office page loads
for path in ["/", "/cut-orders", "/cut-orders?view=all&q=6732", "/cut-orders/new", "/cutting", "/minutes", "/minutes.xlsx",
             "/board", "/setup", "/team", "/activities", "/fixup", "/import", "/programs", "/ledger"]:
    ok(office.get(B + path).status_code == 200, "page " + path)
ok(requests.get(B + "/cut-orders", allow_redirects=False).status_code == 302, "office pages are gated")

# 3. team
for name, pin in (("Ana", "1234"), ("Ben", "5678")):
    office.post(B + "/team", data={"action": "add", "name": name, "pin": pin})
ana, ben = one("select * from team_member where name='Ana'"), one("select * from team_member where name='Ben'")
ok(ana and ben and ana["pin_hash"] and "1234" not in ana["pin_hash"], "team members with hashed PINs")
ok("PIN of 4 to 6" in office.post(B + "/team", data={"action": "add", "name": "Cy", "pin": "12"}).text, "short PIN refused")

# 4. two programs with an H1 time and a yield; one with neither
h1 = one("select id from machine where code='H1'")["id"]
progs = db.execute("""select p.id, p.number, p.units_per_sheet, t.seconds_per_sheet from program p
    join program_time t on t.program_id=p.id and t.machine_id=%s
    where p.units_per_sheet is not null and (select count(*) from program d where d.number=p.number)=1
    order by p.id desc limit 2""", (h1,)).fetchall()
pa, pb = progs
item = one("select i.id from item i join plywood_type t on t.id=i.type_id where i.sheet_size='4x8' and i.thickness='12 mm' and t.name='Txtur'")["id"]
start_stock = one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"]
print("programs:", pa["number"], pa["units_per_sheet"], pa["seconds_per_sheet"], "|", pb["number"], pb["units_per_sheet"], pb["seconds_per_sheet"])

def make(program, qty, so, release=True, item_id=None, machine=h1, sheets=""):
    data = {"program_number": program, "qty_units": qty, "shop_order": so, "job_number": "J" + so,
            "machine_id": machine or "", "sheets_required": sheets, "action": "release" if release else "save"}
    if item_id: data["item_id"] = item_id
    return office.post(B + "/cut-orders/new", data=data, allow_redirects=False)

# 4b. units per sheet read correctly from free text (build 1 misread these)
for txt, want in (("1/2 UNIT", 0.5), ("3/4 OF A UNIT", 0.75), ("2 SHEETS 1 UNIT", 0.5), ("6 PANELS", 6)):
    row = one("select units_per_sheet u from program where units_text=%s limit 1", txt)
    ok(row and abs(float(row["u"]) - want) < 1e-4, f"units per sheet for {txt!r}")
ok(one("select units_per_sheet u from program where units_text='6 OR 9 UNITS' limit 1")["u"] is None, "ambiguous yield left blank")

# 5. the form: program lookup, plywood asked once, sheets worked out
ok(office.get(B + "/api/program", params={"number": pa["number"].lower()}).json()[0]["id"] == pa["id"], "program lookup")
r = make(pa["number"], 10, "7001")
ok(r.status_code == 200 and "needs its plywood" in r.text, "release without plywood is refused")
ok("no program numbered" in make("P9999", 10, "7000").text.lower(), "unknown program refused")
r = make(pa["number"], 10, "7001", item_id=item)
ok(r.status_code == 302 and "/print" in r.headers["Location"], "release goes to the cover sheet")
co1 = one("select * from cut_order where shop_order='7001'")
ok(co1["sheets_required"] == max(1, math.ceil(10 / float(pa["units_per_sheet"]) - 1e-9)), "sheets worked out from the yield")
ok(one("select item_id from program where id=%s", pa["id"])["item_id"] == item, "program remembers its plywood")
r = make(pa["number"], 4, "7002")
ok(r.status_code == 302, "second order on the same program needs no plywood pick")
co2 = one("select * from cut_order where shop_order='7002'")
ok(co2["item_id"] == item, "plywood came from the program")
r = make(pb["number"], 6, "7003", item_id=item); co3 = one("select * from cut_order where shop_order='7003'")
r = make(pa["number"], 3, "7004", release=False); co4 = one("select * from cut_order where shop_order='7004'")
ok(co4["status"] == "filed" and co3["status"] == "queued", "filed and released")
pr = office.get(B + f"/cut-orders/{co1['id']}/print")
ok(pr.status_code == 200 and pr.text.count("<svg") == 2 and "SCAN ON" in pr.text and "SCAN OFF" in pr.text, "cover sheet has two codes")
ok(office.get(B + f"/cut-orders/{co4['id']}/print", allow_redirects=False).status_code == 302, "filed order has no cover sheet yet")

# 6. phone: who are you, PIN, scan on
phone = requests.Session()
r = phone.get(B + f"/s/c/{co1['token']}/on")
ok("Who is this?" in r.text and "Ana" in r.text, "first scan asks who")
r = phone.post(B + f"/s/pin/{ana['id']}", data={"pin": "0000", "next": f"/s/c/{co1['token']}/on"})
ok("not right" in r.text, "wrong PIN refused")
r = phone.post(B + f"/s/pin/{ana['id']}", data={"pin": "1234", "next": f"/s/c/{co1['token']}/on"})
ok("You are ON" in r.text and "Changeover: 15 minutes" in r.text, "scan on, with changeover credit")
s1 = one("select * from scan_session where cut_order_id=%s", co1["id"])
ok(s1["fixed_minutes"] == 15 and s1["machine_id"] == h1 and s1["team_member_id"] == ana["id"], "session recorded")
ok("already on" in phone.get(B + f"/s/c/{co1['token']}/on").text, "second scan on changes nothing")
ok(one("select count(*) n from scan_session where cut_order_id=%s", co1["id"])["n"] == 1, "no duplicate session")

# 7. tap counter
for _ in range(3): phone.post(B + f"/s/tap/{s1['id']}", data={"delta": "1"})
phone.post(B + f"/s/tap/{s1['id']}", data={"delta": "-1"})
ok(one("select count(*) n from sheet_tap where session_id=%s", s1["id"])["n"] == 2, "taps: three on, one off")
other = requests.Session(); other.post(B + f"/s/pin/{ben['id']}", data={"pin": "5678"})
other.post(B + f"/s/tap/{s1['id']}", data={"delta": "1"})
ok(one("select count(*) n from sheet_tap where session_id=%s", s1["id"])["n"] == 2, "nobody can tap someone else's counter")

# 8. scan off: counter pre-fills, count is this time only, stock is deducted
r = phone.get(B + f"/s/c/{co1['token']}/off")
ok('value="2"' in r.text, "scan off is pre-filled from the counter")
ok("Enter how many" in phone.post(B + f"/s/c/{co1['token']}/off", data={"sheets": "two"}).text, "non-number refused")
r = phone.post(B + f"/s/c/{co1['token']}/off", data={"sheets": "1"})
ok("You are OFF" in r.text and "1 sheet recorded" in r.text, "scan off")
co1 = one("select * from cut_order where id=%s", co1["id"])
ok(one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"] == start_stock - 1, "one sheet deducted")
tx = one("select * from inventory_txn where scan_session_id=%s", s1["id"])
ok(tx["txn_type"] == "issue" and tx["shop_order"] == "7001" and tx["job_number"] == "J7001" and tx["team_member"] == "Ana", "ledger line carries job, shop order, team member")
ok("already OFF" in phone.get(B + f"/s/c/{co1['token']}/off").text, "scanning off again straight away says already off")
ok("not scanned onto" in other.get(B + f"/s/c/{co1['token']}/off").text, "scan off when never on")

# 9. changeover: same program back to back earns nothing; a different program earns again; re-scan earns nothing
phone.get(B + f"/s/c/{co2['token']}/on")
ok(one("select fixed_minutes n from scan_session where cut_order_id=%s", co2["id"])["n"] == 0, "same program back to back: no changeover")
phone.get(B + f"/s/c/{co3['token']}/on")
ok(one("select fixed_minutes n from scan_session where cut_order_id=%s", co3["id"])["n"] == 15, "different program: changeover")
phone.get(B + f"/s/c/{co1['token']}/on")
ok(one("select fixed_minutes n from scan_session where cut_order_id=%s order by id desc limit 1", co1["id"])["n"] == 0, "scanning back on: no second changeover")
ok(len(db.execute("select 1 from scan_session where team_member_id=%s and ended_at is null", (ana["id"],)).fetchall()) == 3, "on three cut orders at once")
me = phone.get(B + "/s/me").text
ok(me.count('class="work"') == 3 and "Scan off everything" in me, "My Work shows all three")

# 10. over the open quantity needs a second press; reaching the total completes the order
need2 = co2["sheets_required"]
r = phone.post(B + f"/s/c/{co2['token']}/off", data={"sheets": str(need2 + 2)})
ok("more than" in r.text and one("select ended_at from scan_session where cut_order_id=%s", co2["id"])["ended_at"] is None, "over quantity asks first")
r = phone.post(B + f"/s/c/{co2['token']}/off", data={"sheets": str(need2 + 2), "confirm": str(need2 + 2)})
ok("now complete" in r.text and one("select status from cut_order where id=%s", co2["id"])["status"] == "complete", "confirmed overage completes the order")

# 11. complete and nearly-cut warnings
r = other.get(B + f"/s/c/{co2['token']}/on")
ok("already scanned complete" in r.text and one("select count(*) n from scan_session where cut_order_id=%s and team_member_id=%s", co2["id"], ben["id"])["n"] == 0, "complete order warns before scanning on")
r = other.post(B + f"/s/c/{co2['token']}/on", data={"confirm": "1"})
ok("You are ON" in r.text, "can still scan on after confirming")
other.post(B + f"/s/c/{co2['token']}/off", data={"sheets": "0"})
office.post(B + "/cut-orders/new", data={"program_number": pb["number"], "sheets_required": "10", "qty_units": "", "shop_order": "7005", "machine_id": h1, "item_id": item, "action": "release"})
co5 = one("select * from cut_order where shop_order='7005'")
other.get(B + f"/s/c/{co5['token']}/on"); other.post(B + f"/s/c/{co5['token']}/off", data={"sheets": "9"})
r = phone.get(B + f"/s/c/{co5['token']}/on")
ok("nearly all cut" in r.text and "Only 1 sheet still open" in r.text, "90% cut warns with the open quantity")
ok("not released yet" in phone.get(B + f"/s/c/{co4['token']}/on").text, "filed order cannot be scanned")
ok("not recognised" in phone.get(B + "/s/c/nosuchtoken/on").text, "unknown code")

# 12. poster activities
acts = {a["name"]: a for a in db.execute("select * from activity").fetchall()}
office.post(B + "/activities", data={"action": "save", "id": acts["Clean Up Machine"]["id"], "name": "Clean Up Machine", "earns": "1", "earned_minutes": "10"})
office.post(B + "/activities", data={"action": "add", "name": "Change Spoilboard", "earns": "1", "earned_minutes": "30"})
acts = {a["name"]: a for a in db.execute("select * from activity").fetchall()}
ok(acts["Clean Up Machine"]["earned_minutes"] == 10 and acts["Change Spoilboard"]["earned_minutes"] == 30, "activities maintained")
po = office.get(B + f"/posters/{h1}")
ok(po.status_code == 200 and po.text.count("<svg") == 2 * 6 + 2 and "Change Spoilboard" in po.text, "poster has a pair of codes per activity plus two")
t = acts["Tool Change"]["token"]
r = phone.get(B + f"/s/a/{t}/H1/on"); ok("You are ON Tool Change" in r.text and "Earns 20 minutes" in r.text, "activity on")
ok(one("select fixed_minutes n from scan_session where activity_id=%s", acts["Tool Change"]["id"])["n"] == 20, "activity standard recorded")
r = phone.get(B + f"/s/a/{t}/H1/off"); ok("You are OFF Tool Change" in r.text, "activity off")
w = acts["Waiting on Material"]["token"]
r = phone.get(B + f"/s/a/{w}/H1/on"); ok("does not earn" in r.text, "non-earning activity")
office.post(B + "/activities", data={"action": "toggle", "id": acts["Machine Down"]["id"]})
ok("no longer in use" in phone.get(B + f"/s/a/{acts['Machine Down']['token']}/H1/on").text, "retired code says so")

# 13. scan off everything
r = phone.get(B + "/s/off-all"); ok(r.text.count('name="sheets_') == 2 and "Waiting on Material" in r.text, "off-all lists two cut orders and the activity")
s3 = one("select id from scan_session where cut_order_id=%s and ended_at is null", co3["id"])["id"]
s1b = one("select id from scan_session where cut_order_id=%s and ended_at is null", co1["id"])["id"]
ok("Enter the sheets" in phone.post(B + "/s/off-all", data={f"sheets_{s3}": "2"}).text, "off-all needs every count")
r = phone.post(B + "/s/off-all", data={f"sheets_{s3}": "2", f"sheets_{s1b}": "0"})
ok("OFF everything" in r.text and one("select count(*) n from scan_session where team_member_id=%s and ended_at is null", ana["id"])["n"] == 0, "off-all closes everything")

# 14. typed number fallback
r = phone.post(B + "/s/find", data={"number": f"CO-{co3['id']:04d}", "direction": "on"}); ok("You are ON" in r.text, "typed cut order number")
phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": "0"})

# 14b. the action address never stays in the phone: every scan answers with a redirect to a read-only page
r = phone.get(B + f"/s/c/{co3['token']}/on", allow_redirects=False)
ok(r.status_code == 302 and r.headers["Location"].endswith("/s/done"), "scan on redirects to /s/done")
ok("You are ON" in phone.get(B + "/s/done").text, "/s/done shows the outcome once")
ok("My work" in phone.get(B + "/s/done").text and "You are ON" not in phone.get(B + "/s/done").text, "reloading /s/done shows nothing stale")
ok(one("select count(*) n from scan_session where cut_order_id=%s and team_member_id=%s and ended_at is null", co3["id"], ana["id"])["n"] == 1, "still exactly one open scan")
# the over-quantity confirmation is only good for the number that was warned about
open3 = one("select c.sheets_required - coalesce((select sum(sheets) from scan_session s where s.cut_order_id=c.id),0) n from cut_order c where id=%s", co3["id"])["n"]
r = phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": str(open3 + 1)}); ok("more than" in r.text, "over by one warns")
r = phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": str(open3 + 40), "confirm": str(open3 + 1)})
ok("more than" in r.text and one("select count(*) n from scan_session where cut_order_id=%s and team_member_id=%s and ended_at is null", co3["id"], ana["id"])["n"] == 1, "a different bigger number is warned about again")
ok("as a number" in phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": "\u00b2"}).text, "odd digits refused, not a crash")
ok("as a number" in phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": "99999"}).text, "absurd count refused")
r = phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": "0"}); ok("You are OFF" in r.text, "scan off zero")
before_rows = one("select count(*) n from inventory_txn")["n"]
r = phone.post(B + f"/s/c/{co3['token']}/off", data={"sheets": "0"})
ok("already OFF" in r.text and "Scan on instead" not in r.text, "a second press says already off, without inviting a scan on")
ok(one("select count(*) n from inventory_txn")["n"] == before_rows, "second press posts nothing")
# scan off everything also checks for over-quantity
phone.get(B + f"/s/c/{co3['token']}/on"); sid = one("select id from scan_session where cut_order_id=%s and team_member_id=%s and ended_at is null", co3["id"], ana["id"])["id"]
r = phone.post(B + "/s/off-all", data={f"sheets_{sid}": "500"}); ok("More than was open" in r.text, "off-all warns on over-quantity")
r = phone.post(B + "/s/off-all", data={f"sheets_{sid}": "0", "confirm": f"{sid}:500"}); ok("OFF everything" in r.text, "off-all accepts a corrected number")
# a released cut order must have a plywood; one without cannot be scanned
noitem = db.execute("insert into cut_order (token, status, description, sheets_required, machine_id) values ('tok_noitem','queued','No plywood',5,%s) returning id", (h1,)).fetchone()["id"]
ok("has no plywood set" in phone.get(B + "/s/c/tok_noitem/on").text and one("select count(*) n from scan_session where cut_order_id=%s", noitem)["n"] == 0, "no plywood, no scan")
r = office.post(B + f"/cut-orders/{noitem}/edit", data={"sheets_required": "5", "machine_id": h1, "action": "save"})
ok("needs its plywood" in r.text, "a released order cannot be saved without plywood")
db.execute("delete from cut_order where id=%s", (noitem,))
# no program: no changeover credit
np = db.execute("insert into cut_order (token, status, description, sheets_required, machine_id, item_id) values ('tok_noprog','queued','No program',5,%s,%s) returning id", (h1, item)).fetchone()["id"]
phone.get(B + "/s/c/tok_noprog/on")
ok(one("select fixed_minutes n from scan_session where cut_order_id=%s", np)["n"] == 0, "no program earns no changeover")
phone.post(B + "/s/c/tok_noprog/off", data={"sheets": "0"}); db.execute("delete from scan_session where cut_order_id=%s", (np,)); db.execute("delete from cut_order where id=%s", (np,))
# copy and edit follow a changed quantity
pg = office.get(B + f"/cut-orders/new?copy={co1['id']}").text
ok('id="sheets_required" inputmode="numeric" value=""' in pg, "a copy starts with sheets blank")
ups = float(pa["units_per_sheet"])
office.post(B + f"/cut-orders/{co4['id']}/edit", data={"program_number": pa["number"], "qty_units": "9", "shop_order": "7004", "sheets_required": str(co4["sheets_required"]), "action": "save"})
ok(one("select sheets_required n from cut_order where id=%s", co4["id"])["n"] == math.ceil(9 / ups - 1e-9), "editing the quantity re-works the sheets")
office.post(B + f"/cut-orders/{co4['id']}/edit", data={"program_number": pa["number"], "qty_units": "9", "shop_order": "7004", "sheets_required": "40", "action": "save"})
ok(one("select sheets_required n from cut_order where id=%s", co4["id"])["n"] == 40, "a typed override is kept")
office.post(B + f"/cut-orders/{co4['id']}/edit", data={"program_number": pa["number"], "qty_units": "3", "shop_order": "7004", "sheets_required": "", "action": "save"})
co4 = one("select * from cut_order where id=%s", co4["id"])
# a form posted from another website is refused
ok(office.post(B + "/team", data={"action": "pin", "id": ana["id"], "pin": "9999"}, headers={"Origin": "https://evil.example"}).status_code == 403, "cross-site post refused")
ok(office.post(B + "/team", data={"action": "rename", "id": ana["id"], "name": "Ana"}, headers={"Origin": B}).status_code == 200, "same-site post accepted")
# a changed PIN signs out phones that remembered the old one
p3 = requests.Session(); p3.post(B + f"/s/pin/{ben['id']}", data={"pin": "5678"})
ok("My work" in p3.get(B + "/s/me").text, "Ben's phone is remembered")
office.post(B + "/team", data={"action": "pin", "id": ben["id"], "pin": "5678"})
ok("Who is this?" in p3.get(B + "/s/me").text, "a new PIN signs the old phone out")
other = requests.Session(); other.post(B + f"/s/pin/{ben['id']}", data={"pin": "5678"})

# 15. stock arithmetic: everything deducted equals the sheets on the scans
used = one("select coalesce(sum(sheets),0) n from scan_session")["n"]
ok(one("select coalesce(sum(qty_sheets),0) n from inventory_txn where txn_type='issue'")["n"] == -used, f"ledger issues equal sheets scanned ({used})")
ok(one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"] == start_stock - used, "stock on hand moved by exactly that")

# 16. daily minutes arithmetic, on a controlled day
db.execute("delete from inventory_txn where scan_session_id is not null"); db.execute("delete from sheet_tap"); db.execute("delete from scan_session")
day = date(2026, 10, 5)
def at(h, m=0, d=day): return datetime.combine(d, time(h, m), TZ)
def sess(member, start, end, co=None, act=None, sheets=None, fixed=0):
    return db.execute("insert into scan_session (team_member_id, cut_order_id, activity_id, machine_id, started_at, ended_at, sheets, fixed_minutes, closed_by) values (%s,%s,%s,%s,%s,%s,%s,%s,'self') returning id",
                      (member, co, act, h1, start, end, sheets, fixed)).fetchone()["id"]
sess(ana["id"], at(8), at(10), co=co1["id"], sheets=4, fixed=15)            # 4 sheets on program A
sess(ana["id"], at(9), at(11), co=co3["id"], sheets=3, fixed=15)            # overlaps; 3 sheets on program B
sess(ana["id"], at(11), at(11, 10), act=acts["Tool Change"]["id"], fixed=20)
sess(ana["id"], at(13), at(13, 30), act=acts["Waiting on Material"]["id"], fixed=0)
sess(ben["id"], at(7), at(7, 45), co=co4["id"], sheets=5, fixed=15)         # program A again
nostd = one("select p.id, p.number from program p where not exists (select 1 from program_time t where t.program_id=p.id) and (select count(*) from program d where d.number=p.number)=1 and p.number ~ '^P[0-9]{4}$' limit 1")
office.post(B + "/cut-orders/new", data={"program_number": nostd["number"], "sheets_required": "5", "shop_order": "7006", "machine_id": h1, "item_id": item, "action": "release"})
co6 = one("select * from cut_order where shop_order='7006'")
sess(ben["id"], at(8), at(9), co=co6["id"], sheets=2, fixed=15)             # no standard
sess(ana["id"], at(23, 30, day - timedelta(days=1)), at(0, 30), co=co5["id"], sheets=1, fixed=15)   # crosses midnight
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import floor
res = {p["name"]: p for p in floor.daily_minutes(db, day, now=at(15))}
A, Bn = res["Ana"], res["Ben"]
sa, sb = pa["seconds_per_sheet"], pb["seconds_per_sheet"]
exp_clock = 30 + 180 + 10 + 30          # 00:00-00:30, 08:00-11:00 (overlap once), tool change, waiting
exp_cut = 4 * sa / 60 + 3 * sb / 60 + 1 * sb / 60     # the midnight one ended today on program B
print("Ana:", round(A["clock"], 2), round(A["earn_cut"], 2), A["earn_change"], A["earn_activity"], A["idle"], "| expected", exp_clock, round(exp_cut, 2))
ok(abs(A["clock"] - exp_clock) < 1e-6, "clock counts overlapping scans once")
ok(abs(A["earn_cut"] - exp_cut) < 1e-6, "cutting earned = sheets x standard")
ok(A["earn_change"] == 30 and A["earn_activity"] == 20, "changeovers and activities credited on the day they started")
ok(abs(A["idle"]["Waiting on Material"] - 30) < 1e-6 and A["activities"] == {"Tool Change": 1, "Waiting on Material": 1}, "non-earning time shown by reason")
ok(A["sheets"] == 8 and abs(A["earned"] - (exp_cut + 50)) < 1e-6, "totals")
ok(abs(Bn["clock"] - 105) < 1e-6 and abs(Bn["earn_cut"] - 5 * sa / 60) < 1e-6 and Bn["no_standard"] == [f"CO-{co6['id']:04d}"], "no-standard order earns nothing and is named")
page = text(office.get(B + "/minutes", params={"date": day.isoformat()}))
ok("Ana" in page and "no standard" in page and f"{round(A['ratio'])}%" in page, "daily minutes page shows it")
ok(office.get(B + "/minutes.xlsx", params={"date": day.isoformat()}).status_code == 200, "minutes Excel")

# 17. a forgotten scan is closed back to day end and waits for the supervisor
db.execute("delete from scan_session")
yday = datetime.now(TZ).date() - timedelta(days=1)
fid = db.execute("insert into scan_session (team_member_id, cut_order_id, machine_id, started_at) values (%s,%s,%s,%s) returning id", (ben["id"], co3["id"], h1, at(9, 0, yday))).fetchone()["id"]
live = db.execute("insert into scan_session (team_member_id, cut_order_id, machine_id, started_at) values (%s,%s,%s, now() - interval '20 minutes') returning id", (ana["id"], co1["id"], h1)).fetchone()["id"]
before = one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"]
cut = text(office.get(B + "/cutting"))
f = one("select * from scan_session where id=%s", fid)
ok(f["ended_at"] is not None and f["ended_at"].astimezone(TZ) == at(17, 0, yday) and f["needs_review"] and f["closed_by"] == "auto" and f["sheets"] is None, "forgotten scan closed to 17:00, flagged, no count")
ok(one("select ended_at from scan_session where id=%s", live)["ended_at"] is None and "Ana" in cut and "Ben" not in cut, "today's scan is left alone and shows on Cutting now")
ok(one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"] == before, "nothing deducted until the supervisor enters a count")
ok("Ben" in office.get(B + "/fixup").text, "fix-up lists it")
r = office.post(B + "/fixup", data={"id": fid, "ended_at": at(15, 30, yday).strftime("%Y-%m-%dT%H:%M"), "sheets": "3"})
f = one("select * from scan_session where id=%s", fid)
ok(f["sheets"] == 3 and not f["needs_review"] and f["closed_by"] == "supervisor" and f["ended_at"].astimezone(TZ) == at(15, 30, yday), "fix-up saved")
ok(one("select coalesce(sum(qty_sheets),0) n from inventory_txn where item_id=%s", item)["n"] == before - 3, "fix-up deducted the sheets")

# 18. PIN lockout
p2 = requests.Session()
for _ in range(5): r = p2.post(B + f"/s/pin/{ben['id']}", data={"pin": "0000"})
ok("Too many wrong tries" in r.text, "five wrong PINs lock")
ok("Too many wrong tries" in p2.post(B + f"/s/pin/{ben['id']}", data={"pin": "5678"}).text, "even the right PIN waits out the lock")
db.execute("update team_member set locked_until = now() - interval '1 second' where id=%s", (ben["id"],))
r = p2.post(B + f"/s/pin/{ben['id']}", data={"pin": "0000"})
ok("Wait 10 minutes" in r.text and one("select failed_pins n from team_member where id=%s", ben["id"])["n"] == 6, "the next wrong try doubles the wait; the count is not reset")
ok("locked out" in office.get(B + "/team").text, "the supervisor sees the lockout")
office.post(B + "/team", data={"action": "pin", "id": ben["id"], "pin": "4321"})
ok("My work" in p2.post(B + f"/s/pin/{ben['id']}", data={"pin": "4321"}).text, "supervisor reset clears the lock")

# 19. cut order status buttons, board demand, edit guard
st = lambda cid, action: office.post(B + f"/cut-orders/{cid}/status", data={"action": action, "machine_id": h1})
st(co4["id"], "release"); ok(one("select status from cut_order where id=%s", co4["id"])["status"] == "queued", "release from the detail page")
st(co4["id"], "unrelease"); ok(one("select status from cut_order where id=%s", co4["id"])["status"] == "filed", "back to filed when nothing is cut")
r = st(co1["id"], "cancel"); ok("still scanned onto" in r.text and one("select status from cut_order where id=%s", co1["id"])["status"] == "queued", "cannot cancel while someone is on it")
st(co2["id"], "reopen"); ok(one("select status from cut_order where id=%s", co2["id"])["status"] == "queued", "reopen")
board = text(office.get(B + "/board")); ok("Released" in board and "After filed" in board, "board shows demand")
bq = one("""select sum(greatest(c.sheets_required - coalesce((select sum(sheets) from scan_session s where s.cut_order_id=c.id),0),0)) n
            from cut_order c where c.status='queued' and c.item_id=%s""", item)["n"]
ok(str(int(bq)) in board, f"released demand for the test plywood ({bq}) is on the board")
# 20. nest picture on a program, and Create Cutting from the program page
import io as _io
from PIL import Image, ImageDraw
def picture(size, fmt, mode="RGB"):
    im = Image.new(mode, size, "white"); ImageDraw.Draw(im).rectangle([20, 20, size[0] - 20, size[1] - 20], outline="black", width=6)
    buf = _io.BytesIO(); im.save(buf, fmt); return buf.getvalue()
pid = pa["id"]; purl = B + f"/programs/{pid}"
page = office.get(purl)
ok(page.status_code == 200 and "Create Cutting" in page.text and f"/cut-orders/new?program={pid}" in page.text, "program page has the Create Cutting button")
ok("No nest picture yet" in page.text and office.get(purl + "/nest/picture").status_code == 404, "no nest to start with")
up = lambda name, data: office.post(purl + "/nest", files={"nest": (name, data)})
ok("not a picture or PDF" in up("notes.txt", b"just some words").text and one("select count(*) n from program_nest")["n"] == 0, "a file that is not a picture is refused")
ok("Choose the nest picture" in office.post(purl + "/nest", data={}).text, "upload with no file is refused")
r = up("nests/P1 wide.jpg", picture((3000, 1500), "JPEG"))
n = one("select * from program_nest where program_id=%s", pid)
ok(n and n["filename"] == "P1 wide.jpg" and n["content_type"] == "image/jpeg" and n["width"] == 2200 and n["height"] == 1100 and n["uploaded_by"], "JPG stored, sized down, named without its folder")
pic = office.get(purl + "/nest/picture"); tall = office.get(purl + "/nest/picture?tall=1")
ok(pic.headers["Content-Type"] == "image/jpeg" and Image.open(_io.BytesIO(pic.content)).size == (2200, 1100), "picture is served")
ok(Image.open(_io.BytesIO(tall.content)).size == (1100, 2200), "wide nest stands tall for printing")
orig = office.get(purl + "/nest/file"); ok(orig.status_code == 200 and len(orig.content) == n["size_bytes"], "the original upload comes back whole")
ok(requests.get(purl + "/nest/picture", allow_redirects=False).status_code == 302, "nest pictures are behind office sign-on")
pdf = _io.BytesIO(); Image.new("RGB", (850, 1100), "white").save(pdf, "PDF", save_all=True, append_images=[Image.new("RGB", (850, 1100), "gray")])
up("nest.pdf", pdf.getvalue())
n = one("select * from program_nest where program_id=%s", pid)
ok(one("select count(*) n from program_nest")["n"] == 1 and n["content_type"] == "application/pdf" and n["pages"] == 2 and n["preview_type"] == "image/png" and n["height"] > n["width"], "PDF replaces the JPG; page 1 becomes the picture")
ok(office.get(purl + "/nest/file").headers["Content-Type"] == "application/pdf", "the PDF itself can be opened")
ok("not a picture or PDF" not in up("clear.png", picture((600, 400), "PNG", "RGBA")).text and one("select content_type c from program_nest where program_id=%s", pid)["c"] == "image/png", "PNG with transparency accepted")
ok("could not be opened" in up("bad.pdf", b"%PDF-1.4 nothing here").text and one("select content_type c from program_nest where program_id=%s", pid)["c"] == "image/png", "a broken PDF is refused and the old nest stays")
ok(f">{pa['number']}<" in office.get(B + "/programs?nest=has").text.replace("<b>", ">").replace("</b>", "<") and one("select count(*) n from program_nest")["n"] == 1, "program list can show programs with a nest")
form = office.get(B + f"/cut-orders/new?program={pid}")
src = one("select * from program where id=%s", pid)
ok(form.status_code == 200 and f'value="{src["number"]}"' in form.text and src["name"] in form.text, "Create Cutting opens the form with the program filled in")
ok(f'href="/programs/{pid}"' in form.text and "Started from program" in form.text, "Cancel on that form goes back to the program")
r = office.post(B + "/cut-orders/new", data={"program_number": src["number"], "program_id": pid, "item_id": src["item_id"] or item, "qty_units": 4,
                                             "shop_order": "NEST1", "job_number": "JNEST1", "machine_id": h1, "action": "release"}, allow_redirects=False)
con = one("select * from cut_order where shop_order='NEST1'")
ok(r.status_code == 302 and con and con["program_id"] == pid and con["status"] == "queued", "cutting created from the program")
cover = office.get(B + f"/cut-orders/{con['id']}/print").text
ok(f"/programs/{pid}/nest/picture" in cover and "tall=1" in cover, "cover sheet carries the nest as page 2")
ok(f"/programs/{pid}/nest/picture" in office.get(B + f"/cut-orders/{con['id']}").text, "cut order page shows the nest")
page = office.get(purl).text
ok("CO-%04d" % con["id"] in page and "Cuttings on this program" in page, "program page lists its cuttings")
other = office.get(B + f"/cut-orders/{co2['id']}/print").text
ok(("nest/picture" in other) == bool(one("select 1 from program_nest where program_id=%s", co2["program_id"])), "no nest page when the program has none")
office.post(purl + "/nest/remove")
ok(one("select count(*) n from program_nest")["n"] == 0 and "nest/picture" not in office.get(B + f"/cut-orders/{con['id']}/print").text, "remove takes the nest off the program and the cover sheet")
for path in [f"/cut-orders/{co1['id']}", f"/cut-orders/{co1['id']}/edit", f"/cut-orders/new?copy={co1['id']}", "/cut-orders?view=complete", "/", "/setup"]:
    ok(office.get(B + path).status_code == 200, "page " + path)
if LOG:
    tb = open(LOG).read().count("Traceback")
    ok(tb == 0, f"no server errors in the app log ({tb})")
print(f"ALL {checks} CHECKS PASSED")
