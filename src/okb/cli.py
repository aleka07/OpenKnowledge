import asyncio
import hashlib
import json
import secrets
from pathlib import Path

import typer

from . import db
from .config import settings

app = typer.Typer(help="OpenKnowledge — ingest, search, serve.", no_args_is_help=True)


@app.command()
def init_db():
    """Apply SQL migrations."""
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    with db.connect() as conn:
        applied = db.init_db(conn, migrations)
    typer.echo(f"applied: {', '.join(applied)}")


@app.command()
def ingest(path: Path, source: str = "doc"):
    """Inventory a file or directory: hash, dedup, raw store, enqueue."""
    from .ingest import inventory

    with db.connect() as conn:
        stats = inventory(conn, path.expanduser().resolve(), source=source)
    typer.echo(json.dumps(stats))


@app.command()
def worker(once: bool = typer.Option(False, help="Drain the queue and exit")):
    """Process the ingest queue: normalize -> distill -> embed -> upsert."""
    from .ingest import worker_loop

    with db.connect() as conn:
        asyncio.run(worker_loop(conn, once=once))


@app.command()
def search(q: str, source: str = typer.Option(None), k: int = 10):
    """Debug search from the CLI (same hybrid path as MCP)."""
    from .retrieval import search as hybrid_search

    with db.connect() as conn:
        rows = asyncio.run(hybrid_search(conn, q, sources=[source] if source else None, k=k))
    for r in rows:
        typer.echo(f"[{r['score']:.4f}] {r['source']}/{r['source_id'][:12]} #{r['unit_ord']} "
                   f"{(r['content'] or '')[:120]!r}")


@app.command()
def token_create(name: str):
    """Issue a bearer token (printed once, only the hash is stored)."""
    token = "kb_live_" + secrets.token_urlsafe(24)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO api_tokens (token_hash, name) VALUES (%s,%s)",
            (hashlib.sha256(token.encode()).hexdigest(), name),
        )
    typer.echo(token)


@app.command()
def token_revoke(name: str):
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE api_tokens SET revoked_at=now() WHERE name=%s AND revoked_at IS NULL",
            (name,),
        )
    typer.echo(f"revoked: {cur.rowcount}")


@app.command()
def mcp():
    """Run the MCP server (streamable HTTP)."""
    from .mcp_server import run

    typer.echo(f"MCP on {settings.mcp_host}:{settings.mcp_port} /mcp")
    run()


@app.command()
def status():
    """Queue and evidence counters."""
    with db.connect() as conn:
        jobs = conn.execute(
            "SELECT status, count(*) AS n FROM ingest_jobs GROUP BY status ORDER BY status"
        ).fetchall()
        ev = conn.execute("SELECT count(*) AS n FROM evidence").fetchone()
    typer.echo(json.dumps({"jobs": {r["status"]: r["n"] for r in jobs}, "evidence": ev["n"]}))


def main():
    app()
