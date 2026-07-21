-- query_evidence support: a client-facing view + a read-only DB role.
-- The view hides embedding/tsv (megabytes per row) and lifts common meta
-- fields into columns so the agent writes plain SQL, not jsonb operators.

CREATE OR REPLACE VIEW evidence_v AS
SELECT id, source, source_id, unit, unit_ord, raw_ref,
       content, extracted_text,
       meta->>'doc_type' AS doc_type,
       meta->>'title'    AS title,
       CASE WHEN meta->>'year' ~ '^\d{4}$' THEN (meta->>'year')::int END AS year,
       meta->'authors'   AS authors,
       meta->'people'    AS people,
       meta->'systems'   AS systems,
       meta->>'basis'    AS basis,
       meta,
       occurred_at, indexed_at, superseded_by
FROM evidence;

-- read-only role: physically cannot see api_tokens/query_log or write anything
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'okb_ro') THEN
        CREATE ROLE okb_ro LOGIN PASSWORD 'okb_ro';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO okb_ro;
GRANT SELECT ON evidence_v, raw_objects, source_objects TO okb_ro;
