import asyncio
import datetime as dt
import json
import socket
from pathlib import Path

import psycopg

from . import PIPELINE_VERSION
from . import raw_store
from .classify import PRIORITY, classify, mime_of
from .config import settings
from .db import claim_job, finish_job
from .llm import Artifact, distill_batch, embed_batch
from .normalize import OCR_NOISE_RE, has_meaningful_text, split_chunks, to_markdown

MAX_ATTEMPTS = 3
# One rule: conversational content (meeting episodes) gets length/IDF filtering;
# documents are never filtered — a file someone deliberately saved is signal by
# default, even a one-page drawing. Only degenerate chunks (no meaningful words)
# are dropped.
FILTER_POLICY = "v3:docs-unfiltered"


def inventory(conn: psycopg.Connection, root: Path, source: str = "doc") -> dict:
    """Phase 1: walk, hash, dedup, copy to raw store, enqueue. No LLM work."""
    stats = {"seen": 0, "new": 0, "dup": 0, "queued": 0, "skipped": 0}
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
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

        if kind in ("readable", "scan"):
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
    if obj["kind"] == "scan":
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
    # documents are not length-filtered (see FILTER_POLICY): a short scan with
    # rare tokens (a drawing title, a one-page act) is high-IDF signal, not noise
    chunks = [c for c in split_chunks(md, min_chars=1) if has_meaningful_text(c)]

    if not chunks:
        # name-only fallback: keep the file findable by filename + whatever text
        # it has, instead of silently dropping it from search
        name = Path(occ["path"]).name if occ and occ["path"] else h[:12]
        text = OCR_NOISE_RE.sub("", md).strip()[:1000]
        content = f"{name}\n{text}".strip()
        emb = (await embed_batch([content]))[0]
        conn.execute(
            """INSERT INTO evidence (source, source_id, unit, unit_ord, raw_ref, content,
                   extracted_text, embedding, meta, occurred_at, embedding_model, pipeline_version)
               VALUES (%s,%s,'whole',0,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source, source_id, unit, unit_ord)
               DO UPDATE SET content=excluded.content, extracted_text=excluded.extracted_text,
                   embedding=excluded.embedding, meta=excluded.meta, indexed_at=now()""",
            (source, h, obj["path"], content, md[:4000], str(emb),
             json.dumps({"mime": obj["mime"], "fallback": "name_only",
                         "paths": [occ["path"]] if occ else []}, ensure_ascii=False),
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

    meta_base = {"mime": obj["mime"], "paths": [occ["path"]] if occ else []}
    for i, (chunk, art, emb, content) in enumerate(
        zip(chunks, artifacts, embeddings, contents)
    ):
        meta = dict(
            meta_base,
            people=art.people if art else [],
            systems=art.systems if art else [],
        )
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
        try:
            await process_job(conn, job)
            print(f"job {job['id']} {job['content_hash'][:12]} done")
        except Exception as e:  # poison file must not stop the pipeline
            status = "failed" if job["attempts"] >= MAX_ATTEMPTS else "pending"
            finish_job(conn, job["id"], status, f"{type(e).__name__}: {e}")
            print(f"job {job['id']} {status}: {e}")
