"""Dry-run inventory of an archive: hashes, kinds, dedup, PDF pages.

Read-only reconnaissance before a backfill: no DB writes, no raw store copies,
no LLM calls. Prints a summary and saves full JSON next to it.
"""

import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from okb.classify import classify  # noqa: E402

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "~/kb-data/mirrors/pcf-24-26").expanduser()
OUT = Path("/tmp/dry-inventory.json")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def probe(path_str: str):
    p = Path(path_str)
    try:
        size = p.stat().st_size
        kind = classify(p)
        pages = None
        if p.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader

                pages = len(PdfReader(p).pages)
            except Exception:
                pages = None
        with open(p, "rb") as f:
            magic = f.read(5)
        rtf_as_doc = p.suffix.lower() == ".doc" and magic == b"{\\rtf"
        return {"path": path_str, "hash": sha256(p), "size": size, "kind": kind,
                "ext": p.suffix.lower(), "pages": pages, "rtf_as_doc": rtf_as_doc}
    except Exception as e:
        return {"path": path_str, "hash": None, "size": 0, "kind": "probe_error",
                "ext": p.suffix.lower(), "pages": None, "rtf_as_doc": False,
                "error": str(e)[:120]}


def main():
    files = [str(p) for p in ROOT.rglob("*")
             if p.is_file() and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)]
    print(f"files to probe: {len(files)}", flush=True)

    results = []
    with ProcessPoolExecutor(8) as ex:
        for i, r in enumerate(ex.map(probe, files, chunksize=16)):
            results.append(r)
            if i and i % 1000 == 0:
                print(f"  probed {i}", flush=True)

    OUT.write_text(json.dumps(results, ensure_ascii=False))

    by_hash = {}
    for r in results:
        if r["hash"]:
            by_hash.setdefault(r["hash"], []).append(r)
    uniq = [v[0] for v in by_hash.values()]
    dup_files = sum(len(v) - 1 for v in by_hash.values())
    dup_bytes = sum(v[0]["size"] * (len(v) - 1) for v in by_hash.values())

    kinds = Counter(r["kind"] for r in uniq)
    kind_bytes = Counter()
    for r in uniq:
        kind_bytes[r["kind"]] += r["size"]
    scan_pages = sum(r["pages"] or 0 for r in uniq if r["kind"] == "scan")
    readable_pdf_pages = sum(r["pages"] or 0 for r in uniq if r["kind"] == "readable" and r["ext"] == ".pdf")
    other_exts = Counter(r["ext"] for r in uniq if r["kind"] == "other").most_common(10)
    image_exts = Counter(r["ext"] for r in uniq if r["kind"] == "image").most_common(5)
    rtf_docs = sum(1 for r in uniq if r["rtf_as_doc"])
    errors = [r for r in results if r["kind"] == "probe_error"]
    largest = sorted(uniq, key=lambda r: -r["size"])[:5]

    print(json.dumps({
        "total_files": len(files),
        "unique_blobs": len(uniq),
        "duplicate_files": dup_files,
        "duplicate_mb": round(dup_bytes / 1e6),
        "kinds": dict(kinds),
        "kind_mb": {k: round(v / 1e6) for k, v in kind_bytes.items()},
        "scan_pdf_pages": scan_pages,
        "readable_pdf_pages": readable_pdf_pages,
        "rtf_disguised_as_doc": rtf_docs,
        "other_exts_top": other_exts,
        "image_exts_top": image_exts,
        "probe_errors": len(errors),
        "largest": [{"p": r["path"].split("/")[-1][:50], "mb": round(r["size"] / 1e6)} for r in largest],
    }, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
