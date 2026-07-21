import logging
import re
from pathlib import Path

from .config import settings

# pdfminer (inside MarkItDown) is extremely chatty about broken font descriptors
logging.getLogger("pdfminer").setLevel(logging.ERROR)

MAX_CHUNK_CHARS = 3500
MIN_CHUNK_CHARS = 200  # filter_policy v1: shorter units are noise, skipped

_HEADING_RE = re.compile(r"^#{1,3} ", re.MULTILINE)


def _docling_pdf(path: Path) -> str:
    """Readable PDFs go through docling-serve: unlike pdfminer it recovers real
    heading structure, so chunks follow document sections.

    Not used for docx/xlsx/pptx (MarkItDown output is equal or better and its
    tables are compact — docling pads table cells with megabytes of spaces) nor
    for scans (docling OCR garbles Cyrillic; the VLM path handles those).
    """
    import httpx

    r = httpx.post(
        f"{settings.docling_url}/v1/convert/file",
        files={"files": (path.name, path.read_bytes())},
        data={"to_formats": "md"},
        timeout=600,
    )
    r.raise_for_status()
    md = r.json()["document"]["md_content"]
    if not md.strip():
        raise ValueError("empty docling output")
    return re.sub(r" {3,}", "  ", md)  # collapse column-alignment padding


def to_markdown(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(errors="replace")
    if suffix == ".pdf":
        try:
            return _docling_pdf(path)
        except Exception:
            pass  # docling container down/erroring -> plain-text fallback below
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content


# OCR service markers and placeholders carry no meaning of their own
OCR_NOISE_RE = re.compile(r"<!--[^>]*-->|\(empty page\)|\(page failed OCR\)")


def has_meaningful_text(text: str, min_letters: int = 10) -> bool:
    return sum(ch.isalpha() for ch in OCR_NOISE_RE.sub("", text)) >= min_letters


def split_chunks(md: str, min_chars: int = MIN_CHUNK_CHARS) -> list[str]:
    """Split by top-level headings, then greedily pack to MAX_CHUNK_CHARS.

    Oversized heading-less sections fall back to paragraph packing.
    """
    positions = [m.start() for m in _HEADING_RE.finditer(md)]
    sections = (
        [md[a:b] for a, b in zip([0, *positions], [*positions, len(md)])]
        if positions
        else [md]
    )

    pieces: list[str] = []
    for sec in sections:
        if len(sec) <= MAX_CHUNK_CHARS:
            pieces.append(sec)
            continue
        for para in re.split(r"\n{2,}", sec):
            while len(para) > MAX_CHUNK_CHARS:
                pieces.append(para[:MAX_CHUNK_CHARS])
                para = para[MAX_CHUNK_CHARS:]
            pieces.append(para)

    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) > MAX_CHUNK_CHARS:
            chunks.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        chunks.append(buf)

    return [c.strip() for c in chunks if len(c.strip()) >= min_chars]
