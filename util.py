"""Small shared helpers: cleaning up legacy spellings, time formats, labels."""
import datetime
import os
import re
from fractions import Fraction
from zoneinfo import ZoneInfo

PLANT_TZ = ZoneInfo(os.environ.get("TPMS_TZ", "America/New_York"))

_ACRONYMS = {"OFS", "LVL", "MDF", "OSB", "CDX"}
_BLANKS = {"", "N/A", "N//A", "N /A", "M/A", "N/N", "?", "NONE", "SEE NOTES"}


def norm_ws(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def is_blank(value):
    return norm_ws(value).upper() in _BLANKS


def norm_thickness(value):
    """'12 MM.' -> '12 mm', '7/8 IN.' -> '7/8"', '1  3/8"' -> '1 3/8"'."""
    t = norm_ws(value)
    if is_blank(t):
        return ""
    t = re.sub(r"\s*MM\.?(?![A-Za-z])", " mm", t, flags=re.I)
    t = re.sub(r"\s*(?:IN\.?|INCH(?:ES)?)$", '"', t, flags=re.I)
    t = re.sub(r'\s+"', '"', t)
    t = t.replace(" OR ", " or ")
    return norm_ws(t)


def norm_size(value):
    """'4X8' -> '4x8'. Odd sizes are kept as written."""
    t = norm_ws(value)
    if is_blank(t):
        return ""
    return re.sub(r"(?<=\d)\s*[Xx]\s*(?=\d)", "x", t)


def norm_machine(value):
    """'H 1' -> 'H1'."""
    t = re.sub(r"\s+", "", norm_ws(value)).upper()
    return t if re.fullmatch(r"[A-Z]{1,6}\d{1,2}", t) else ""


def norm_status(value):
    t = norm_ws(value).upper()
    if t.startswith("APP"):
        return "approved"
    if t.startswith("SAM"):
        return "sample"
    if t.startswith("RETIRE"):
        return "retired"
    return "unknown"


def parse_number(value):
    """A positive number from a cell that may hold '6 UNITS', '2 = 1 UNIT', 0.5 ... else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value > 0 else None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", norm_ws(value))
    if not m:
        return None
    n = float(m.group(1))
    return n if n > 0 else None


def parse_time_cell(value):
    """Run time from the legacy program list, as seconds per sheet.

    The sheet's times are minutes:seconds, but Excel stored them as hours:minutes,
    so 10:15:00 means 10 min 15 s and '1 day, 0:05:00' means 24 min 5 s.
    Text like '7:28/6:30' takes the first figure. Returns None when unreadable.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return None
    if isinstance(value, datetime.time):
        secs = value.hour * 60 + value.minute
        return secs or None
    if isinstance(value, datetime.timedelta):
        secs = int(round(value.total_seconds() / 60))
        return secs or None
    text = norm_ws(value).split("/")[0].strip()
    m = re.fullmatch(r"(\d*):(\d{1,2})(?::00)?", text)
    if not m:
        return None
    secs = int(m.group(1) or 0) * 60 + int(m.group(2))
    return secs or None


def fmt_secs(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_mmss(text):
    """Form input: '5:30' or '5' (minutes) or '5.5' (minutes). '' -> None. Raises ValueError."""
    t = norm_ws(text)
    if not t:
        return None
    m = re.fullmatch(r"(\d+):(\d{1,2})", t)
    if m:
        secs = int(m.group(1)) * 60 + int(m.group(2))
    else:
        secs = int(round(float(t) * 60))
    if secs <= 0:
        raise ValueError("time must be more than zero")
    return secs


def thickness_mm(thickness):
    """Sort key so 6 mm < 12 mm < 3/4" < 18 mm."""
    t = norm_ws(thickness)
    try:
        if "mm" in t.lower():
            return float(re.findall(r"\d+(?:\.\d+)?", t)[-1])
        parts = t.replace('"', "").split()
        return float(sum(Fraction(p) for p in parts)) * 25.4
    except (ValueError, IndexError, ZeroDivisionError):
        return 9999.0


def item_label(row):
    label = f"{row['sheet_size']} {row['thickness']} {row['type_name']}"
    if row.get("grade"):
        label += f" · {row['grade']}"
    return label


def item_sort_key(row):
    return (row["sheet_size"], thickness_mm(row["thickness"]), row["type_name"].lower(), row.get("grade") or "")


def title_type(text):
    """'OFS OAK' -> 'OFS Oak', 'BIRCH B/BB' -> 'Birch B/BB'."""
    out = []
    for tok in norm_ws(text).split():
        out.append(tok.upper() if "/" in tok or tok.upper() in _ACRONYMS else tok.capitalize())
    return " ".join(out)


def make_code(sheet_size, thickness, type_name, grade=""):
    """Short code such as '4x8-12mm-TXTUR'. Editable on the item master."""
    def clean(part):
        return re.sub(r"[^A-Za-z0-9]", "", part or "")
    parts = [clean(sheet_size).lower(), clean(thickness).lower(),
             clean(type_name).upper()[:14], clean(grade).upper()[:8]]
    return "-".join(p for p in parts if p)
