import asyncio
import datetime as dt
import json
import re
import socket
from pathlib import Path

import psycopg

from . import PIPELINE_VERSION
from . import raw_store
from .classify import PRIORITY, classify, mime_of
from .config import settings
from .db import claim_job, finish_job
from .llm import Artifact, distill_batch, doc_passport, embed_batch
from .normalize import OCR_NOISE_RE, has_meaningful_text, split_chunks, to_markdown
from .translit import fold_names

MAX_ATTEMPTS = 3
# One rule: conversational content (meeting episodes) gets length/IDF filtering;
# documents are never filtered — a file someone deliberately saved is signal by
# default, even a one-page drawing. Only degenerate chunks (no meaningful words)
# are dropped.
FILTER_POLICY = "v3:docs-unfiltered"


def inventory(conn: psycopg.Connection, root: Path, source: str = "doc",
              limit: int | None = None) -> dict:
    """Phase 1: walk, hash, dedup, copy to raw store, enqueue. No LLM work.

    limit caps how many NEW files get enqueued (batch mode); already-seen
    files don't count against it, so re-running walks past them for free.
    """
    stats = {"seen": 0, "new": 0, "dup": 0, "queued": 0, "skipped": 0}
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        if limit is not None and stats["queued"] >= limit:
            break
        if path.name.startswith("."):
            continue
        stats["seen"] += 1
        h = raw_store.sha256_file(path)
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        external_id = str(path)

        if conn.execute("SELECT 1 FROM raw_objects WHERE content_hash=%s", (h,)).fetchone():
            conn.execute(
                """INSERT INTO source_objects (source, external_id, content_hash, path, modified_at)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (source, external_id)
                   DO UPDATE SET content_hash=excluded.content_hash, path=excluded.path,
                                 modified_at=excluded.modified_at, deleted_at=NULL""",
                (source, external_id, h, str(path), mtime),
            )
            stats["dup"] += 1
            continue

        kind = classify(path)
        ref = raw_store.put(h, path)
        conn.execute(
            "INSERT INTO raw_objects (content_hash, mime, bytes, path, kind) VALUES (%s,%s,%s,%s,%s)",
            (h, mime_of(path), path.stat().st_size, ref, kind),
        )
        conn.execute(
            """INSERT INTO source_objects (source, external_id, content_hash, path, modified_at)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (source, external_id)
               DO UPDATE SET content_hash=excluded.content_hash, path=excluded.path,
                             modified_at=excluded.modified_at, deleted_at=NULL""",
            (source, external_id, h, str(path), mtime),
        )
        stats["new"] += 1

        # images are queued too: they get a cheap name-only evidence row
        if kind in ("readable", "scan", "image"):
            conn.execute(
                """INSERT INTO ingest_jobs (content_hash, stage, priority, filter_policy)
                   VALUES (%s,'normalize',%s,%s)""",
                (h, PRIORITY[kind], FILTER_POLICY),
            )
            stats["queued"] += 1
        else:
            # image / other — recorded, not processed (photo content deferred in v3)
            conn.execute(
                """INSERT INTO ingest_jobs (content_hash, stage, priority, status, error)
                   VALUES (%s,'normalize',%s,'skipped',%s)""",
                (h, PRIORITY[kind], f"kind={kind} not processed in v1 slice"),
            )
            stats["skipped"] += 1
    return stats


NOTE_PROJECT_RE = re.compile(r"^Project: (.+)$", re.MULTILINE)


def note_project(md: str) -> str | None:
    """Notes carry their project as a header line (add_note writes it):
    unlike mirror documents, a note's filesystem path says nothing about
    what the note is about."""
    m = NOTE_PROJECT_RE.search(md[:500])
    return m.group(1).strip() if m else None


def project_of(path: str | None) -> str | None:
    """Folder-derived search scope: file's directory relative to its mirror root.

    Folders already encode topics («Зерде», «KazNetCOM 2025»), so they become
    search scopes for free. Derived from the path, not stored knowledge —
    after a folder rename `okb reproject` re-stamps it without touching the
    expensive pipeline (content hash didn't change, so nothing re-processes).
    """
    if not path:
        return None
    parts = Path(path).parts
    if "mirrors" not in parts:
        return None
    i = parts.index("mirrors")
    return "/".join(parts[i + 2:-1]) or None


async def _passport_safe(filename: str, md: str) -> dict:
    """Doc-level passport as a meta patch; empty dict when extraction fails.

    Retries matter: a single unretried call during a vLLM restart window left
    39% of the first backfill without doc_type — silently.
    """
    for _ in range(3):
        try:
            p = await doc_passport(filename, md[:7000])
            fields = {"doc_type": p.doc_type, "title": p.title, "year": p.year,
                      "authors": p.authors or None, "basis": p.basis}
            return {k: v for k, v in fields.items() if v is not None}
        except Exception:
            await asyncio.sleep(5)
    return {}


def _render_content(a: Artifact) -> str:
    parts = [a.summary]
    if a.question:
        parts.append(f"Q: {a.question}")
    if a.resolution:
        status = f" [{a.resolution_status}]" if a.resolution_status else ""
        parts.append(f"Resolution{status}: {a.resolution}")
    return "\n".join(parts)


async def process_job(conn: psycopg.Connection, job: dict) -> None:
    h = job["content_hash"]
    obj = conn.execute("SELECT * FROM raw_objects WHERE content_hash=%s", (h,)).fetchone()
    occ = conn.execute(
        """SELECT source, path, modified_at FROM source_objects
           WHERE content_hash=%s ORDER BY modified_at DESC NULLS LAST LIMIT 1""",
        (h,),
    ).fetchone()
    source = occ["source"] if occ else "doc"

    original = raw_store.resolve(obj["path"])
    if obj["kind"] == "image":
        # v3: photo content is not processed — the name-only fallback below
        # keeps the file findable by filename and folder path
        md = ""
    elif obj["kind"] == "scan":
        from .ocr import scan_to_markdown

        converted = original.parent / "converted.md"
        if converted.exists():
            md = converted.read_text()  # OCR is cached: policy re-runs cost no GPU
        else:
            md = await scan_to_markdown(original)
            # derived first-level artifact: kept next to the original, recomputable
            converted.write_text(md)
    else:
        md = to_markdown(original)
        if original.suffix.lower() not in {".md", ".txt"}:
            # derived first-level artifact for every binary format: get_raw
            # serves it, and passport re-runs read it instead of re-converting
            (original.parent / "converted.md").write_text(md)
    # documents are not length-filtered (see FILTER_POLICY): a short scan with
    # rare tokens (a drawing title, a one-page act) is high-IDF signal, not noise
    chunks = [c for c in split_chunks(md, min_chars=1) if has_meaningful_text(c)]

    doc_name = Path(occ["path"]).name if occ and occ["path"] else h[:12]
    passport = {} if obj["kind"] == "image" else await _passport_safe(doc_name, md)

    if not chunks:
        # name-only fallback: keep the file findable by filename + whatever text
        # it has, instead of silently dropping it from search
        name = Path(occ["path"]).name if occ and occ["path"] else h[:12]
        # folder names carry meaning ("Командировки/Иссыккуль/...") — include them
        rel = "/".join(Path(occ["path"]).parts[-4:-1]) if occ and occ["path"] else ""
        text = OCR_NOISE_RE.sub("", md).strip()[:1000]
        content = "\n".join(x for x in (name, rel, text) if x)
        emb = (await embed_batch([content]))[0]
        conn.execute(
            """INSERT INTO evidence (source, source_id, unit, unit_ord, raw_ref, content,
                   extracted_text, embedding, meta, occurred_at, embedding_model, pipeline_version)
               VALUES (%s,%s,'whole',0,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source, source_id, unit, unit_ord)
               DO UPDATE SET content=excluded.content, extracted_text=excluded.extracted_text,
                   embedding=excluded.embedding, meta=excluded.meta, indexed_at=now()""",
            (source, h, obj["path"], content, md[:4000], str(emb),
             json.dumps({"mime": obj["mime"], "fallback": "name_only", **passport,
                         **({"people_norm": fold_names(list(passport.get("authors") or []))}
                            if passport.get("authors") else {}),
                         "paths": [occ["path"]] if occ else [],
                         **({"project": p} if (p := project_of(occ["path"] if occ else None)) else {})},
                        ensure_ascii=False),
             occ["modified_at"] if occ else None,
             settings.embedding_model_tag, PIPELINE_VERSION),
        )
        finish_job(conn, job["id"], "done")
        return

    artifacts = await distill_batch(chunks)
    # failed chunks fall back to raw text: still searchable, marked in meta
    contents = [
        _render_content(a) if a else chunk[:1500]
        for a, chunk in zip(artifacts, chunks)
    ]
    embeddings = await embed_batch(contents)

    meta_base = {"mime": obj["mime"], "paths": [occ["path"]] if occ else [], **passport}
    if p := project_of(occ["path"] if occ else None):
        meta_base["project"] = p
    elif source == "note" and (p := note_project(md)):
        meta_base["project"] = p
    for i, (chunk, art, emb, content) in enumerate(
        zip(chunks, artifacts, embeddings, contents)
    ):
        meta = dict(
            meta_base,
            people=art.people if art else [],
            systems=art.systems if art else [],
        )
        names = (art.people if art else []) + list(passport.get("authors") or [])
        if names:
            meta["people_norm"] = fold_names(names)
        if art is None:
            meta["distill"] = "failed_passthrough"
        elif art.resolution_status:
            meta["resolution_status"] = art.resolution_status
        conn.execute(
            """INSERT INTO evidence (source, source_id, unit, unit_ord, raw_ref, content,
                   extracted_text, embedding, meta, occurred_at, embedding_model, pipeline_version)
               VALUES (%s,%s,'chunk',%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source, source_id, unit, unit_ord)
               DO UPDATE SET raw_ref=excluded.raw_ref, content=excluded.content,
                   extracted_text=excluded.extracted_text, embedding=excluded.embedding,
                   meta=excluded.meta, occurred_at=excluded.occurred_at,
                   embedding_model=excluded.embedding_model,
                   pipeline_version=excluded.pipeline_version, indexed_at=now(),
                   superseded_by=NULL""",
            (source, h, i, obj["path"], content, chunk, str(emb),
             json.dumps(meta, ensure_ascii=False),
             occ["modified_at"] if occ else None,
             settings.embedding_model_tag, PIPELINE_VERSION),
        )

    # re-chunking may yield fewer chunks than a previous run: drop stale tails
    conn.execute(
        "DELETE FROM evidence WHERE source=%s AND source_id=%s AND unit='chunk' AND unit_ord >= %s",
        (source, h, len(chunks)),
    )

    # supersede evidence of older content versions of the same logical file(s)
    conn.execute(
        """UPDATE evidence e SET superseded_by = (
               SELECT id FROM evidence WHERE source_id = %(h)s LIMIT 1)
           WHERE e.superseded_by IS NULL AND e.source_id <> %(h)s AND e.source_id IN (
               SELECT so_old.content_hash FROM source_objects so_new
               JOIN source_objects so_old
                 ON so_old.source = so_new.source AND so_old.external_id = so_new.external_id
               WHERE so_new.content_hash = %(h)s AND so_old.content_hash <> %(h)s)""",
        {"h": h},
    )
    finish_job(conn, job["id"], "done")


async def _extend_lease(conn: psycopg.Connection, job_id: int) -> None:
    """Heartbeat for long jobs (multi-hour monster docs): without it the 10-min
    lease expires mid-work and other workers 'steal' the same job — all workers
    end up chewing one document while the queue stalls."""
    while True:
        await asyncio.sleep(240)
        conn.execute(
            "UPDATE ingest_jobs SET lease_until = now() + interval '10 minutes' WHERE id = %s",
            (job_id,),
        )


async def worker_loop(conn: psycopg.Connection, once: bool = False) -> None:
    name = f"{socket.gethostname()}:{id(conn)}"
    idle = 0
    while True:
        job = claim_job(conn, name)
        if job is None:
            if once:
                return
            idle += 1
            await asyncio.sleep(min(30, 2 * idle))
            continue
        idle = 0
        heartbeat = asyncio.create_task(_extend_lease(conn, job["id"]))
        try:
            await process_job(conn, job)
            print(f"job {job['id']} {job['content_hash'][:12]} done")
        except Exception as e:  # poison file must not stop the pipeline
            status = "failed" if job["attempts"] >= MAX_ATTEMPTS else "pending"
            finish_job(conn, job["id"], status, f"{type(e).__name__}: {e}")
            print(f"job {job['id']} {status}: {e}")
        finally:
            heartbeat.cancel()
