import json

import psycopg

from .llm import embed_query

# Hybrid retrieve: vector top-N + full-text top-N in one statement,
# fused with RRF(60), freshness decay on top. Superseded rows excluded.
SEARCH_SQL = """
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rnk
    FROM evidence
    WHERE superseded_by IS NULL AND embedding IS NOT NULL
      AND (%(sources)s::text[] IS NULL OR source = ANY(%(sources)s))
      AND (%(after)s::timestamptz IS NULL OR occurred_at >= %(after)s)
      AND (%(people)s::text[] IS NULL OR meta->'people' ?| %(people)s)
      AND (%(project)s::text IS NULL OR meta->>'project' LIKE %(project)s || '%%')
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT 40
),
fts AS (
    SELECT id, row_number() OVER (
        ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('russian', %(q)s)) DESC) AS rnk
    FROM evidence
    WHERE superseded_by IS NULL
      AND tsv @@ websearch_to_tsquery('russian', %(q)s)
      AND (%(sources)s::text[] IS NULL OR source = ANY(%(sources)s))
      AND (%(after)s::timestamptz IS NULL OR occurred_at >= %(after)s)
      AND (%(people)s::text[] IS NULL OR meta->'people' ?| %(people)s)
      AND (%(project)s::text IS NULL OR meta->>'project' LIKE %(project)s || '%%')
    LIMIT 40
),
fused AS (
    SELECT coalesce(vec.id, fts.id) AS id,
           coalesce(1.0/(60+vec.rnk), 0) + coalesce(1.0/(60+fts.rnk), 0) AS rrf
    FROM vec FULL OUTER JOIN fts USING (id)
),
scored AS (
    SELECT e.id, e.source, e.source_id, e.unit, e.unit_ord, e.content,
           e.extracted_text, e.meta, e.occurred_at, e.raw_ref,
           f.rrf * (0.7 + 0.3 * exp(-0.005 * coalesce(
               extract(epoch FROM now() - e.occurred_at)/86400.0, 365))) AS score
    FROM fused f JOIN evidence e ON e.id = f.id
)
-- per-document cap: one long document must not fill the whole top-k with
-- its own chunks; each document competes with its 2 best chunks only
SELECT id, source, source_id, unit, unit_ord, content, extracted_text,
       meta, occurred_at, raw_ref, score
FROM (
    SELECT s.*, row_number() OVER (
        PARTITION BY s.source_id ORDER BY s.score DESC) AS doc_rn
    FROM scored s
) capped
WHERE doc_rn <= 2
ORDER BY score DESC
LIMIT %(k)s;
"""


async def search(
    conn: psycopg.Connection,
    q: str,
    sources: list[str] | None = None,
    project: str | None = None,
    people: list[str] | None = None,
    after: str | None = None,
    k: int = 10,
) -> list[dict]:
    qvec = await embed_query(q)
    rows = conn.execute(
        SEARCH_SQL,
        {
            "q": q,
            "qvec": str(qvec),
            "sources": sources,
            "project": project,
            "people": people,
            "after": after,
            "k": k,
        },
    ).fetchall()
    for r in rows:
        r["id"] = str(r["id"])
        r["score"] = float(r["score"])
        if r["occurred_at"]:
            r["occurred_at"] = r["occurred_at"].isoformat()
        # extracted_text can be large; return a preview, get_raw serves the full original
        if r["extracted_text"] and len(r["extracted_text"]) > 600:
            r["extracted_text"] = r["extracted_text"][:600] + "…"
    return rows


def recent(conn: psycopg.Connection, source: str | None = None, days: int = 7) -> list[dict]:
    rows = conn.execute(
        """SELECT id, source, source_id, unit, unit_ord, content, meta, occurred_at, indexed_at
           FROM evidence
           WHERE indexed_at >= now() - interval '1 day' * %s
             AND (%s::text IS NULL OR source = %s)
             AND superseded_by IS NULL
           ORDER BY indexed_at DESC LIMIT 50""",
        (days, source, source),
    ).fetchall()
    for r in rows:
        r["id"] = str(r["id"])
        for f in ("occurred_at", "indexed_at"):
            if r[f]:
                r[f] = r[f].isoformat()
    return rows


def log_query(conn: psycopg.Connection, token_name: str | None, tool: str,
              query: str | None, rows: list[dict]) -> None:
    conn.execute(
        "INSERT INTO query_log (token_name, tool, query, top_ids) VALUES (%s,%s,%s,%s)",
        (token_name, tool, query, json.dumps([r.get("id") for r in rows])),
    )
