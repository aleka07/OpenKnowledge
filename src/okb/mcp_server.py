import hashlib

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
    """Hybrid (vector + full-text) search over the whole knowledge base."""
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
    rows = await hybrid_search(conn(), q, sources=["doc", "obsidian", "archive"], project=project)
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
