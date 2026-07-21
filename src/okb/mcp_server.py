import datetime as dt
import hashlib
import re

import psycopg
from psycopg.rows import dict_row

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import db, raw_store
from .config import settings
from .retrieval import log_query, recent as recent_rows, search as hybrid_search

mcp = FastMCP("openknowledge")
_conn = None


def conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = db.connect()
    return _conn


def _token_name() -> str | None:
    try:
        return get_http_request().state.token_name
    except Exception:
        return None


@mcp.tool
async def search(q: str, sources: list[str] | None = None, project: str | None = None,
                 k: int = 10) -> list[dict]:
    """Find knowledge by MEANING or exact phrase (hybrid vector + full-text, top-k).

    Use for "where was X discussed", "what do we know about Y".
    Do NOT use for enumerations or counting ("list all", "how many") — it returns
    the top-k most similar rows, not a complete set; use query_evidence for that.
    """
    rows = await hybrid_search(conn(), q, sources=sources, project=project, k=k)
    log_query(conn(), _token_name(), "search", q, rows)
    return rows


@mcp.tool
async def search_meetings(q: str, after: str | None = None,
                          people: list[str] | None = None) -> list[dict]:
    """Search meeting transcripts; filter by date (ISO) and participants."""
    rows = await hybrid_search(conn(), q, sources=["meeting"], after=after, people=people)
    log_query(conn(), _token_name(), "search_meetings", q, rows)
    return rows


@mcp.tool
async def search_docs(q: str, project: str | None = None) -> list[dict]:
    """Search documents: Drive, Obsidian, archive, inbox files."""
    rows = await hybrid_search(conn(), q, sources=["doc", "obsidian", "archive", "note"], project=project)
    log_query(conn(), _token_name(), "search_docs", q, rows)
    return rows


@mcp.tool
def get_raw(raw_ref: str, offset: int = 0, length: int = 20000) -> str:
    """Verbatim original (or a byte range of it) from the raw store.

    For binary originals (e.g. scanned PDFs) the derived converted.md is
    served instead — it keeps <!-- page N --> markers, so quotes stay addressable.
    """
    path = raw_store.resolve(raw_ref)
    converted = path.parent / "converted.md"
    if path.suffix.lower() not in {".md", ".txt"} and converted.exists():
        path = converted
    data = path.read_bytes()[offset : offset + length]
    log_query(conn(), _token_name(), "get_raw", raw_ref, [])
    return data.decode("utf-8", errors="replace")


@mcp.tool
def recent(source: str | None = None, days: int = 7) -> list[dict]:
    """What was indexed recently."""
    rows = recent_rows(conn(), source=source, days=days)
    log_query(conn(), _token_name(), "recent", None, rows)
    return rows


MAX_SQL_ROWS = 200


@mcp.tool
def query_evidence(sql: str) -> dict:
    """Deterministic SELECT over the knowledge base — enumerate, count, aggregate.

    Use for "list ALL", "how many", "group by year", facet discovery. Guarantees
    completeness within the extracted attributes; rows where an attribute was not
    determined have NULL there — check `WHERE doc_type IS NULL` to see blind spots.
    Do NOT use for semantic questions — that is search().

    Schema (read-only):
      evidence_v(id, source, source_id, unit, unit_ord, raw_ref, content,
                 extracted_text, doc_type, title, year, authors, people, systems,
                 basis, meta, occurred_at, indexed_at, superseded_by)
        -- one row per chunk; one document = rows sharing source_id.
        -- doc_type vocabulary: paper report contract proposal invoice order
        --   certificate presentation instruction letter note other
      raw_objects(content_hash, mime, bytes, path, first_seen, kind)
      source_objects(source, external_id, content_hash, path, modified_at, deleted_at)

    Examples:
      SELECT DISTINCT ON (source_id) title, year, raw_ref FROM evidence_v
        WHERE doc_type='paper' AND year=2026 AND superseded_by IS NULL;
      SELECT doc_type, count(DISTINCT source_id) FROM evidence_v GROUP BY 1;
      SELECT title, raw_ref FROM evidence_v WHERE meta->'paths' @> '["..."]';

    Filter superseded_by IS NULL unless you want history. Max 200 rows returned.
    """
    q = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", q) or ";" in q:
        return {"error": "single SELECT/WITH statement only"}
    try:
        with psycopg.connect(
            settings.db_ro_dsn, row_factory=dict_row, autocommit=True,
            options="-c statement_timeout=5000 -c default_transaction_read_only=on",
        ) as ro:
            cur = ro.execute(q)
            rows = cur.fetchmany(MAX_SQL_ROWS)
            truncated = cur.fetchone() is not None
    except psycopg.Error as e:
        return {"error": str(e).strip()}
    out = [
        {k: (v.isoformat() if isinstance(v, dt.datetime) else
             str(v) if isinstance(v, dt.date) else
             str(v) if v.__class__.__name__ == "UUID" else v)
         for k, v in r.items()}
        for r in rows
    ]
    log_query(conn(), _token_name(), "query_evidence", sql, [])
    return {"rows": out, "row_count": len(out), "truncated": truncated}


@mcp.tool
def add_note(title: str, text: str, sources: list[str] | None = None) -> str:
    """Save a cross-document finding as a note in the knowledge base.

    ONLY call after the user explicitly confirmed saving THIS note in this
    conversation. The note must be self-contained: state the finding, the
    reasoning, and reference source documents (raw_ref or paths) so every claim
    is checkable. Notes are regular documents (source='note'), attributed to the
    caller's token; originals are never modified. To retract or correct one,
    save a newer note — do not rewrite history.
    """
    from .ingest import inventory

    author = _token_name() or "unknown"
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[:60] or "note"
    notes_dir = settings.data_dir.expanduser() / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{stamp}-{slug}.md"
    n = 1
    while path.exists():
        n += 1
        path = notes_dir / f"{stamp}-{slug}-{n}.md"
    src_lines = "\n".join(f"- {s}" for s in (sources or []))
    path.write_text(
        f"# {title}\n\nAuthor: {author} (via add_note)\nDate: {stamp}\n\n"
        f"{text}\n\n## Sources\n\n{src_lines or '- (none given)'}\n"
    )
    stats = inventory(conn(), path, source="note")
    log_query(conn(), _token_name(), "add_note", title, [])
    return f"saved {path.name}, queued for indexing ({stats})"


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        token_hash = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
        row = conn().execute(
            "SELECT name FROM api_tokens WHERE token_hash=%s AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        request.state.token_name = row["name"]
        return await call_next(request)


def run() -> None:
    import uvicorn

    app = mcp.http_app(path="/mcp")
    app.add_middleware(BearerAuth)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
