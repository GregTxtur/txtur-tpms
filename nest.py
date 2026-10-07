"""Nest pictures: turn whatever was uploaded (a photo, a scan, a PDF) into one picture TPMS can show and print."""
import io

import pypdfium2 as pdfium
from PIL import Image, ImageOps

MAX_BYTES = 15 * 1024 * 1024
PREVIEW_MAX = 2200          # longest side in pixels: sharp on a letter page, small enough to load quickly
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp",
               "BMP": "image/bmp", "TIFF": "image/tiff"}


class NestError(ValueError):
    """The upload could not be used; the message is fit to show the person who uploaded it."""


def read_upload(data):
    """Returns a dict: content_type, preview (bytes), preview_type, width, height, pages. Raises NestError."""
    if not data:
        raise NestError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise NestError("That file is over 15 MB. Save a smaller copy and try again.")
    pages = 1
    if data[:1024].lstrip().startswith(b"%PDF-"):
        try:
            pdf = pdfium.PdfDocument(data)
            pages = len(pdf)
            page = pdf[0]
            width, height = page.get_size()
            image = page.render(scale=min(PREVIEW_MAX / max(width, height), 4.0)).to_pil().convert("RGB")
        except Exception as exc:  # noqa: BLE001 - any unreadable PDF gets the same answer
            raise NestError("That PDF could not be opened. Try saving it again, or upload a picture instead.") from exc
        content_type, lossless = "application/pdf", True
    else:
        try:
            image = Image.open(io.BytesIO(data))
            fmt = image.format
            image.load()
        except Exception as exc:  # noqa: BLE001
            raise NestError("That file is not a picture or PDF that TPMS can read. Use a JPG, PNG or PDF.") from exc
        if fmt not in IMAGE_TYPES:
            raise NestError("That kind of picture is not supported. Use a JPG, PNG or PDF.")
        content_type, lossless = IMAGE_TYPES[fmt], fmt != "JPEG"
        image = ImageOps.exif_transpose(image)          # phone photos carry their rotation as a note
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGBA")
            flat = Image.new("RGB", image.size, "white")  # transparent drawings print on white
            flat.paste(image, mask=image.split()[-1])
            image = flat
        image.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
    buf = io.BytesIO()
    if lossless:
        image.save(buf, "PNG", optimize=True)
        preview_type = "image/png"
    else:
        image.convert("RGB").save(buf, "JPEG", quality=88)
        preview_type = "image/jpeg"
    return {"content_type": content_type, "preview": buf.getvalue(), "preview_type": preview_type,
            "width": image.width, "height": image.height, "pages": pages}


def upright(preview, preview_type):
    """The preview turned to stand tall, so a wide nest fills a portrait page when printed."""
    image = Image.open(io.BytesIO(preview))
    if image.width <= image.height:
        return preview
    image = image.rotate(90, expand=True)
    buf = io.BytesIO()
    if preview_type == "image/png":
        image.save(buf, "PNG", optimize=True)
    else:
        image.save(buf, "JPEG", quality=88)
    return buf.getvalue()
