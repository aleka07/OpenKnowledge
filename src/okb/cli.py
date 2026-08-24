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
def ingest(path: Path, source: str = "doc",
           limit: int = typer.Option(None, help="Stop after enqueueing N new files")):
    """Inventory a file or directory: hash, dedup, raw store, enqueue."""
    from .ingest import inventory

    with db.connect() as conn:
        stats = inventory(conn, path.expanduser().resolve(), source=source, limit=limit)
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
def passport(missing_only: bool = typer.Option(False, help="Only docs without a passport (no basis in meta)")):
    """Re-extract doc-level passports into evidence.meta — no re-distill, cheap."""
    from . import raw_store
    from .ingest import _passport_safe
    from .normalize import to_markdown

    async def run(conn):
        cond = "WHERE NOT (e.meta ? 'basis')" if missing_only else ""
        docs = conn.execute(
            f"""SELECT DISTINCT ON (e.source_id) e.source_id, r.path AS raw_path, so.path AS src_path
               FROM evidence e
               JOIN raw_objects r ON r.content_hash = e.source_id
               LEFT JOIN source_objects so ON so.content_hash = e.source_id
               {cond}"""
        ).fetchall()

        async def one(d):
            original = raw_store.resolve(d["raw_path"])
            conv = original.parent / "converted.md"
            if conv.exists():
                md = conv.read_text()
            elif original.suffix.lower() in {".md", ".txt"}:
                md = original.read_text(errors="replace")
            else:
                md = to_markdown(original)
            name = Path(d["src_path"]).name if d["src_path"] else original.name
            return d["source_id"], await _passport_safe(name, md)

        for sid, patch in await asyncio.gather(*[one(d) for d in docs]):
            if patch:
                conn.execute(
                    "UPDATE evidence SET meta = meta || %s::jsonb WHERE source_id = %s",
                    (json.dumps(patch, ensure_ascii=False), sid),
                )
            typer.echo(f"{sid[:12]} {patch or 'no passport'}")

    with db.connect() as conn:
        asyncio.run(run(conn))


@app.command()
def admin():
    """Serve the admin web UI."""
    from .admin import run

    run()


@app.command()
def admin_password():
    """Hash a password for KB_ADMIN_PASSWORD_HASH (prompts, prints .env lines)."""
    from .admin import hash_password

    pw = typer.prompt("Admin password", hide_input=True, confirmation_prompt=True)
    typer.echo(f"KB_ADMIN_PASSWORD_HASH={hash_password(pw)}")
    typer.echo(f"KB_ADMIN_SECRET={secrets.token_urlsafe(32)}")


@app.command()
def normnames():
    """Backfill meta.people_norm (transliterated names) for existing rows."""
    from .translit import fold_names

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, meta FROM evidence WHERE meta ? 'people' OR meta ? 'authors'"
        ).fetchall()
        n = 0
        for r in rows:
            names = list(r["meta"].get("people") or []) + list(r["meta"].get("authors") or [])
            norm = fold_names(names)
            if norm and norm != r["meta"].get("people_norm"):
                conn.execute(
                    "UPDATE evidence SET meta = meta || jsonb_build_object('people_norm', %s::text) WHERE id = %s",
                    (norm, r["id"]),
                )
                n += 1
    typer.echo(f"updated {n} of {len(rows)} rows")


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
