import hashlib
import shutil
from pathlib import Path

from .config import settings


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def put(content_hash: str, src: Path) -> str:
    """Copy original into the raw store; return path relative to data_dir."""
    dest_dir = settings.raw_dir / content_hash[:2] / content_hash
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"original{src.suffix.lower()}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return str(dest.relative_to(settings.data_dir.expanduser()))


def resolve(raw_ref: str) -> Path:
    """Resolve a raw_ref to an absolute path, refusing escapes from data_dir."""
    base = settings.data_dir.expanduser().resolve()
    path = (base / raw_ref).resolve()
    if not path.is_relative_to(base):
        raise ValueError("raw_ref escapes data dir")
    return path
