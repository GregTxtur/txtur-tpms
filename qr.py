"""QR codes for cut orders and posters, and the address they point at."""
import io
import os

import segno
from flask import request
from markupsafe import Markup


def public_base():
    """Where phones should reach TPMS. TPMS_PUBLIC_URL wins; otherwise the address in use, forced to https
    off a developer machine (the app sits behind nginx and only sees plain http)."""
    configured = os.environ.get("TPMS_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    host = request.host
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    return ("http://" if local else "https://") + host


def scan_url(path):
    return public_base() + path


def svg(data, scale=6):
    """Inline SVG for a QR code. Medium error correction: survives a smudge or a crease."""
    buf = io.BytesIO()
    # omitsize: the SVG carries a viewBox instead of a fixed size, so the page's CSS decides how big it prints.
    segno.make(data, error="m").save(buf, kind="svg", scale=scale, border=2, xmldecl=False, svgns=True, nl=False,
                                     omitsize=True)
    return Markup(buf.getvalue().decode("utf-8"))
