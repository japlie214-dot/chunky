"""Single page renderer and screenshot column helpers."""
from __future__ import annotations
import io
from .constants import SCREENSHOT_DPI, SCREENSHOT_MAX_BYTES, CORTEX_IMAGE_MAX_EDGE

_JPEG_LADDER = (92, 85, 75, 65, 55, 45)

def _encode_within(image, budget=SCREENSHOT_MAX_BYTES) -> bytes:
    from PIL import Image
    scale = 1.0
    last = b""
    for _ in range(5):
        img = image if scale == 1 else image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        img.save(stream, format="PNG", optimize=True)
        last = stream.getvalue()
        if len(last) <= budget:
            return last
        rgb = img.convert("RGB") if img.mode in ("RGBA", "P", "LA") else img
        for quality in _JPEG_LADDER:
            stream = io.BytesIO()
            rgb.save(stream, format="JPEG", quality=quality, optimize=True)
            last = stream.getvalue()
            if len(last) <= budget:
                return last
        scale *= .8
    return last

def render_page(pdf_bytes: bytes, page: int, dpi: int = SCREENSHOT_DPI) -> bytes:
    from PIL import Image
    from pdf2image import convert_from_bytes
    from .poppler_bootstrap import get_poppler_bin_or_raise
    images = convert_from_bytes(pdf_bytes, first_page=page, last_page=page, dpi=dpi,
                                poppler_path=get_poppler_bin_or_raise())
    if not images:
        raise RuntimeError(f"poppler returned no image for page {page}")
    image = images[0]
    longest = max(image.width, image.height)
    if longest > CORTEX_IMAGE_MAX_EDGE:
        ratio = CORTEX_IMAGE_MAX_EDGE / longest
        image = image.resize((int(image.width * ratio), int(image.height * ratio)),
                             Image.Resampling.LANCZOS)
    return _encode_within(image)

def screenshot_for_page(session, log, db, schema, table, pdf_name, page):
    import base64
    rows = log.execute(f"SELECT BASE64_ENCODE(PAGE_SCREENSHOT) AS B64 FROM "
                       f'{__import__("chunky_utils._shared", fromlist=["qualify"]).qualify(db, schema, table)} '
                       "WHERE PDF_NAME = ? AND PAGE_NUMBER = ? AND PAGE_SCREENSHOT IS NOT NULL LIMIT 1",
                       params=[pdf_name, page])
    if rows and rows[0].get("B64"):
        return base64.b64decode(rows[0]["B64"]), "column"
    return None, "missing"
