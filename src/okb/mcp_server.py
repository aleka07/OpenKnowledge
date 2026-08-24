import datetime as dt
import hashlib
import json
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

mcp = FastMCP(
    "openknowledge",
    instructions=(
        "Team knowledge base: work documents from Nextcloud plus distilled "
        "notes written by teammates' agents.\n\n"
        "Reading: use search for meaning-questions ('what do we know about X', "
        "'кто настраивал Y'); use query_evidence (SQL) for enumeration and "
        "counting. sources=['note'] limits to teammates' notes, "
        "sources=['archive'] to documents; project='<folder path prefix>' "
        "scopes search to a folder.\n\n"
        "Writing back (add_note) — this KB is two-way. When you and your user "
        "finish something significant (a decision made, something deployed or "
        "configured, a non-obvious pitfall found), OFFER the user to save a "
        "distilled note: what was done and decided, where it runs "
        "(hosts/paths), which systems were touched, who was involved (names). "
        "Show the note text and save only after the user agrees. Write a "
        "summary someone can act on a year later, not a dialog recap. NEVER "
        "include passwords, tokens or other secrets. Authorship is attributed "
        "automatically via your token.\n\n"
        "Give notes a project: reuse an existing one (from search results' "
        "meta.project, or via list_projects) rather than inventing a new "
        "name. Continuing earlier work = a new note in the same project, "
        "never a rewrite of old notes."
    ),
)
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
MAX_FIELD_CHARS = 800
MAX_RESPONSE_CHARS = 50_000


@mcp.tool
def query_evidence(sql: str) -> dict:
    """Deterministic SELECT over the knowledge base — enumerate, count, aggregate.

    Use for "list ALL", "how many", "group by year", facet discovery.
    Do NOT use for semantic questions — that is search().

    Schema (read-only), with types:
      evidence_v(
        id uuid, source text, source_id text, unit text, unit_ord int,
        raw_ref text, content text, extracted_text text,
        doc_type text,      -- model-extracted: strong hint, NOT ground truth;
                            --   vocabulary: paper report contract proposal invoice
                            --   order certificate presentation instruction letter
                            --   note other; NULL = classification missing
        title text, year int,
        authors jsonb,      -- json ARRAYS: cast ::text for ILIKE, or use @>
        people jsonb, systems jsonb,
        people_norm text,   -- ALL names (people+authors) transliterated to
                            --   lowercase latin, space-joined — search names HERE
        basis text,         -- where doc_type/year came from (provenance)
        meta jsonb, occurred_at timestamptz, indexed_at timestamptz,
        superseded_by uuid)
        -- one row per chunk; one document = all rows sharing source_id
      raw_objects(content_hash text PK, mime text, bytes bigint, path text,
                  first_seen timestamptz, kind text)
      source_objects(source text, external_id text, content_hash text,
                     path text, modified_at timestamptz, deleted_at timestamptz)

    Rules that prevent silently wrong answers:
    - NAMES: one person = many spellings (Baurzhan/Bauyrzhan/Бауыржан). Always
      fuzzy-match the transliterated field:
        WHERE word_similarity('baurzhan', people_norm) > 0.5
      Plain ILIKE on people/authors WILL miss spelling variants.
    - DUPLICATES: the same document may exist as pdf+docx+md with different
      source_id — collapse lists with DISTINCT ON (title) or GROUP BY title.
    - BLIND ZONE: attributes are model-extracted; before claiming "all X",
      check SELECT count(DISTINCT source_id) FROM evidence_v WHERE doc_type IS NULL.
    - Add superseded_by IS NULL unless you want history.

    Examples:
      SELECT DISTINCT ON (title) title, year, raw_ref FROM evidence_v
        WHERE doc_type='paper' AND year=2026 AND superseded_by IS NULL;
      SELECT title FROM evidence_v
        WHERE word_similarity('baurzhan', people_norm) > 0.5 AND doc_type='paper';
      SELECT title FROM evidence_v WHERE authors @> '["Alikhan Amirkhanov"]';
      SELECT doc_type, count(DISTINCT source_id) FROM evidence_v GROUP BY 1;

    Max 200 rows; long text fields truncated to 800 chars (fetch full text via
    get_raw); total response capped at ~50k chars with truncated=true flag.
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
    def clean(v):
        if isinstance(v, dt.datetime):
            return v.isoformat()
        if isinstance(v, dt.date) or v.__class__.__name__ in ("UUID", "Decimal"):
            return str(v)
        if isinstance(v, str) and len(v) > MAX_FIELD_CHARS:
            return v[:MAX_FIELD_CHARS] + "…[truncated, use get_raw]"
        return v

    out, budget = [], MAX_RESPONSE_CHARS
    for r in rows:
        row = {k: clean(v) for k, v in r.items()}
        budget -= len(json.dumps(row, ensure_ascii=False, default=str))
        if budget < 0:
            truncated = True
            break
        out.append(row)
    log_query(conn(), _token_name(), "query_evidence", sql, [])
    return {"rows": out, "row_count": len(out), "truncated": truncated}


@mcp.tool
def list_projects(q: str | None = None, limit: int = 30) -> list[dict]:
    """Existing projects (topic scopes), most recently active first.

    Call BEFORE choosing a project for add_note or a scoped search: pass
    q="vllm" to find close matches instead of inventing a duplicate name.
    Without q returns the most active projects, not necessarily all of them.
    """
    rows = conn().execute(
        """SELECT meta->>'project' AS project,
                  count(DISTINCT source_id) FILTER (WHERE source != 'note') AS docs,
                  count(DISTINCT source_id) FILTER (WHERE source = 'note') AS notes,
                  max(indexed_at)::date AS last_activity
           FROM evidence
           WHERE meta->>'project' IS NOT NULL AND superseded_by IS NULL
             AND (%(q)s::text IS NULL OR meta->>'project' ILIKE '%%' || %(q)s || '%%')
           GROUP BY 1 ORDER BY max(indexed_at) DESC LIMIT %(limit)s""",
        {"q": q, "limit": max(1, min(limit, 200))},
    ).fetchall()
    log_query(conn(), _token_name(), "list_projects", q, [])
    return [dict(r, last_activity=str(r["last_activity"])) for r in rows]


@mcp.tool
def add_note(title: str, text: str, sources: list[str] | None = None,
             project: str | None = None) -> str:
    """Save a distilled finding as a note the whole team can search later.

    WHEN to offer: after finishing something significant with the user — a
    decision made, something deployed/configured, a non-obvious pitfall found,
    a cross-document finding. Proactively suggest saving; don't wait to be
    asked.

    ONLY call after the user explicitly confirmed saving THIS note in this
    conversation — show them the text first. Include: what was done and
    decided, where it runs (hosts, paths), which systems were touched, who was
    involved (names make people-search work). Reference source documents
    (raw_ref or paths) where relevant. Write a summary someone can act on a
    year later, not a dialog recap. NEVER include passwords, tokens or other
    secrets.

    `project` attaches the note to a topic so scoped search finds it. Prefer
    an EXISTING project: copy meta.project from the search results you worked
    with, or check list_projects(q=...) for a close match; invent a new short
    name only when nothing fits. Continuing earlier work = a NEW note in the
    same project — never re-save or rewrite old notes' content.

    Notes are regular documents (source='note'), attributed to the caller's
    token; originals are never modified. To retract or correct one, save a
    newer note — do not rewrite history.
    """
    from .ingest import inventory

    author = _token_name() or "unknown"
    # provenance the server can vouch for itself: client app + source IP
    try:
        req = get_http_request()
        ua = req.headers.get("user-agent", "?")
        ip = (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (req.client.host if req.client else "?"))
        client_line = f"{ua} from {ip}"
    except Exception:
        client_line = "?"
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
        f"# {title}\n\nAuthor: {author} (via add_note)\nDate: {stamp}\n"
        f"Client: {client_line}\n"
        + (f"Project: {project.strip()}\n" if project and project.strip() else "")
        + "\n"
        f"{text}\n\n## Sources\n\n{src_lines or '- (none given)'}\n"
    )
    stats = inventory(conn(), path, source="note")
    log_query(conn(), _token_name(), "add_note", title, [])
    return f"saved {path.name}, queued for indexing ({stats})"


# the onboarding guide is public by design: it contains no secrets, and the
# whole point is that a teammate's agent can read it before having a token
PUBLIC_PATHS = {"/connect", "/connect.md"}


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
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
    from starlette.routing import Route

    from .admin import connect_md, connect_page

    app = mcp.http_app(path="/mcp")
    app.router.routes.append(Route("/connect", connect_page))
    app.router.routes.append(Route("/connect.md", connect_md))
    app.add_middleware(BearerAuth)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
