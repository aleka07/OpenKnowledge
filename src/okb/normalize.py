import logging
import re
from pathlib import Path

# pdfminer (inside MarkItDown) is extremely chatty about broken font descriptors
logging.getLogger("pdfminer").setLevel(logging.ERROR)

MAX_CHUNK_CHARS = 3500
MIN_CHUNK_CHARS = 200  # filter_policy v1: shorter units are noise, skipped

_HEADING_RE = re.compile(r"^#{1,3} ", re.MULTILINE)


def to_markdown(path: Path) -> str:
    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(errors="replace")
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content


def split_chunks(md: str) -> list[str]:
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

    return [c.strip() for c in chunks if len(c.strip()) >= MIN_CHUNK_CHARS]
