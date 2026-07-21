import mimetypes
from pathlib import Path

READABLE_EXTS = {
    ".md", ".txt", ".rst", ".docx", ".xlsx", ".pptx", ".html", ".htm",
    ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".doc", ".rtf", ".xls",  # legacy formats: .doc/.rtf go through LibreOffice
    ".docm",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff"}

# scan detection threshold: fewer chars per page than this = no text layer
MIN_CHARS_PER_PAGE = 100

PRIORITY = {"readable": 5, "scan": 10, "image": 20, "other": 20}


def _pdf_kind(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        pages = reader.pages[:5]
        if not pages:
            return "other"
        chars = sum(len((p.extract_text() or "")) for p in pages)
        return "readable" if chars / len(pages) >= MIN_CHARS_PER_PAGE else "scan"
    except Exception:
        return "other"


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in READABLE_EXTS:
        return "readable"
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return _pdf_kind(path)
    return "other"


def mime_of(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
