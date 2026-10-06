"""Re-read "units in 1 sheet" for programs build 1 loaded from the workbook.

Build 1 took the leading number of that free-text column, so '1/2 UNIT' became 1 and
'3/4 OF A UNIT' became 3, which would under-count the sheets a cut order needs. This corrects
only programs still holding the value build 1 gave them; anything edited by hand since is left alone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from util import parse_number, parse_units_per_sheet  # noqa: E402


def run(conn):
    rows = conn.execute("select id, units_text, units_per_sheet from program where units_text <> ''").fetchall()
    changed = 0
    for row in rows:
        program_id, text, current = (row["id"], row["units_text"], row["units_per_sheet"]) if isinstance(row, dict) else row
        old = parse_number(text)
        new = parse_units_per_sheet(text)
        same_as_build_1 = (current is None and old is None) or (
            current is not None and old is not None and abs(float(current) - float(old)) < 0.00005)
        if same_as_build_1 and (old or 0) != (new or 0):
            conn.execute("update program set units_per_sheet = %s, updated_at = now() where id = %s", (new, program_id))
            changed += 1
    return changed
