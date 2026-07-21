import asyncio
import base64
from pathlib import Path

from .llm import ocr_page

DPI = 150


async def _page_safe(b64: str) -> str | None:
    """Page-level isolation: a broken page must not fail the whole scan."""
    for _ in range(3):
        try:
            return await ocr_page(b64)
        except Exception:
            pass
    return None


async def scan_to_markdown(path: Path) -> str:
    """Scanned PDF -> per-page VLM transcription -> merged markdown.

    Page markers are kept so get_raw can address a specific page later.
    """
    import pymupdf

    doc = pymupdf.open(path)
    try:
        pages = [
            base64.b64encode(page.get_pixmap(dpi=DPI).tobytes("png")).decode()
            for page in doc
        ]
    finally:
        doc.close()

    mds = await asyncio.gather(*[_page_safe(b64) for b64 in pages])
    out = []
    for i, md in enumerate(mds, start=1):
        body = md if md is not None else "(page failed OCR)"
        out.append(f"<!-- page {i} -->\n\n{body}")
    return "\n\n".join(out)
