-- Fuzzy name search: pg_trgm over transliterated names (meta.people_norm).
-- 'baurzhan' must match rows mentioning Bauyrzhan / Бауыржан etc.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS evidence_people_norm_trgm
    ON evidence USING gin ((meta->>'people_norm') gin_trgm_ops);

-- view gains people_norm mid-list -> must be dropped and recreated (re-grant below)
DROP VIEW IF EXISTS evidence_v;
CREATE VIEW evidence_v AS
SELECT id, source, source_id, unit, unit_ord, raw_ref,
       content, extracted_text,
       meta->>'doc_type' AS doc_type,
       meta->>'title'    AS title,
       CASE WHEN meta->>'year' ~ '^\d{4}$' THEN (meta->>'year')::int END AS year,
       meta->'authors'   AS authors,
       meta->'people'    AS people,
       meta->'systems'   AS systems,
       meta->>'people_norm' AS people_norm,
       meta->>'basis'    AS basis,
       meta,
       occurred_at, indexed_at, superseded_by
FROM evidence;

GRANT SELECT ON evidence_v TO okb_ro;
