"""Pipeline status, shared by the admin panel and the MCP server.

The admin dashboard needs the full operator picture (error texts, LLM
endpoints, doc types); agents over MCP only need to answer "is the base
up to date, is an update running right now" — public_status() is that
subset.
"""

import subprocess

import httpx

from . import db
from .config import settings

MIRROR_DIR = "mirrors/pcf-cd-24-26"


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception as e:
        return f"error: {e}"


def _active_units(pattern: str) -> list[str]:
    out = _systemctl("list-units", "--plain", "--no-legend", pattern)
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def pipeline_units() -> list[str]:
    """Transient units doing pipeline work right now (workers, ingest, refresh)."""
    return (_active_units("okb-worker*") + _active_units("okb-ingest-*")
            + _active_units("okb-refresh-*"))


def sync_active() -> bool:
    return _systemctl("is-active", "okb-nextcloud-sync.service") == "active"


def _endpoint_alive(url: str) -> bool:
    try:
        return httpx.get(url + "/models", timeout=3).status_code == 200
    except Exception:
        return False


def _mirror_remaining() -> dict:
    root = settings.data_dir.expanduser() / MIRROR_DIR
    try:
        total = sum(1 for p in root.rglob("*")
                    if p.is_file() and not p.name.startswith("."))
    except OSError:
        total = 0
    with db.connect() as conn:
        seen = conn.execute(
            "SELECT count(*) n FROM source_objects WHERE path LIKE %s",
            (str(root) + "/%",)).fetchone()["n"]
    return {"mirror_total": total, "mirror_remaining": max(0, total - seen)}


def gather_status() -> dict:
    """Everything the admin dashboard shows. Operator-only detail included."""
    with db.connect() as conn:
        jobs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) n FROM ingest_jobs GROUP BY status")}
        ev = conn.execute(
            "SELECT count(*) chunks, count(DISTINCT source_id) docs FROM evidence"
        ).fetchone()
        passthrough = conn.execute(
            "SELECT count(*) n FROM evidence WHERE meta->>'distill'='failed_passthrough'"
        ).fetchone()["n"]
        no_passport = conn.execute(
            """SELECT count(DISTINCT source_id) n FROM evidence e
               WHERE NOT EXISTS (SELECT 1 FROM evidence x
                   WHERE x.source_id=e.source_id AND x.meta ? 'basis')"""
        ).fetchone()["n"]
        doc_types = conn.execute(
            """SELECT coalesce(meta->>'doc_type','—') dt, count(DISTINCT source_id) n
               FROM evidence GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        # by-design skips (junk kinds) are counted, not listed — only real
        # failures and operator-parked jobs deserve the errors panel
        errors = conn.execute(
            """SELECT id, status, left(error, 200) error, updated_at
               FROM ingest_jobs
               WHERE error IS NOT NULL
                 AND NOT (status = 'skipped' AND error LIKE 'kind=%')
               ORDER BY updated_at DESC LIMIT 20"""
        ).fetchall()
        junk_skipped = conn.execute(
            """SELECT count(*) n FROM ingest_jobs
               WHERE status = 'skipped' AND error LIKE 'kind=%'"""
        ).fetchone()["n"]
        feed = conn.execute(
            """SELECT date_trunc('day', indexed_at)::date AS d,
                      count(DISTINCT source_id) AS n
               FROM evidence GROUP BY 1 ORDER BY 1 DESC LIMIT 7"""
        ).fetchall()
    return {
        **_mirror_remaining(),
        "feed": [{"d": str(r["d"]), "n": r["n"]} for r in feed],
        "feed_max": max((r["n"] for r in feed), default=1),
        "junk_skipped": junk_skipped,
        "jobs": jobs,
        "chunks": ev["chunks"], "docs": ev["docs"],
        "passthrough": passthrough,
        "passthrough_pct": round(100 * passthrough / ev["chunks"], 1) if ev["chunks"] else 0,
        "no_passport": no_passport,
        "doc_types": [dict(r) for r in doc_types],
        "errors": [dict(r, updated_at=str(r["updated_at"])[:19]) for r in errors],
        "workers": pipeline_units(),
        "sync_state": _systemctl("is-active", "okb-nextcloud-sync.service"),
        "gen_alive": _endpoint_alive(settings.gen_url),
        "emb_alive": _endpoint_alive(settings.emb_url),
        "gen_model": settings.gen_model,
    }


def public_status() -> dict:
    """Agent-facing subset: coarse state, queue depth, freshness.

    No error texts, no endpoint health, no unit names — that is operator
    detail for the admin panel.
    """
    with db.connect() as conn:
        jobs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) n FROM ingest_jobs GROUP BY status")}
        ev = conn.execute(
            "SELECT count(DISTINCT source_id) docs, max(indexed_at) last FROM evidence"
        ).fetchone()
        week = conn.execute(
            """SELECT count(DISTINCT source_id) n FROM evidence
               WHERE indexed_at > now() - interval '7 days'"""
        ).fetchone()["n"]
    if sync_active():
        state = "syncing"
    elif pipeline_units() or jobs.get("processing", 0):
        state = "processing"
    elif jobs.get("pending", 0):
        state = "queued"
    else:
        state = "idle"
    return {
        "state": state,
        "queue": {
            "pending": jobs.get("pending", 0),
            "processing": jobs.get("processing", 0),
            "failed": jobs.get("failed", 0),
            "backlog_files": _mirror_remaining()["mirror_remaining"],
        },
        "docs": ev["docs"],
        "last_indexed": str(ev["last"])[:19] if ev["last"] else None,
        "indexed_last_7d": week,
    }
