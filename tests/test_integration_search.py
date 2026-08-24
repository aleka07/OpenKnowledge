"""Live-DB invariants of the search pipeline. Run on gx10-1: KB_IT=1 uv run pytest

These are the checks we otherwise did by hand after every batch.
"""

import asyncio

import pytest

from okb import db
from okb.retrieval import search

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn():
    with db.connect() as c:
        yield c


def _search(conn, **kw):
    return asyncio.run(search(conn, **kw))


def test_per_document_cap(conn):
    rows = _search(conn, q="договор поставки товара", k=10)
    per_doc = {}
    for r in rows:
        per_doc[r["source_id"]] = per_doc.get(r["source_id"], 0) + 1
    assert per_doc and max(per_doc.values()) <= 2


def test_project_prefix_scope(conn):
    rows = _search(conn, q="договор", k=10, project="2025")
    assert rows
    for r in rows:
        assert (r["meta"].get("project") or "").startswith("2025")


def test_scope_mismatch_returns_empty(conn):
    assert _search(conn, q="договор", k=5, project="no-such-folder") == []


def test_no_orphan_embeddings(conn):
    n = conn.execute(
        """SELECT count(*) AS n FROM evidence
           WHERE embedding IS NULL AND superseded_by IS NULL
             AND unit != 'whole'"""
    ).fetchone()["n"]
    assert n == 0


def test_every_doc_has_paths(conn):
    n = conn.execute(
        "SELECT count(*) AS n FROM evidence WHERE meta->'paths' IS NULL"
    ).fetchone()["n"]
    assert n == 0


def test_passthrough_share_is_low(conn):
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE meta->>'distill'='failed_passthrough') AS bad,
                  count(*) AS total FROM evidence"""
    ).fetchone()
    assert row["total"] > 0
    # the 2026-08 reset happened at 41%; alert well before that
    assert row["bad"] / row["total"] < 0.05


def test_reproject_is_idempotent(conn):
    import json
    import subprocess

    out = subprocess.run(
        ["uv", "run", "okb", "reproject"], capture_output=True, text=True, timeout=300
    )
    assert out.returncode == 0
    stats = json.loads(out.stdout.strip().splitlines()[-1])
    assert stats["updated"] == 0  # nothing moved since the last run
