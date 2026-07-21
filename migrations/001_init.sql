-- OpenKnowledge v3 — initial schema (Doc 2, section 01).
-- Five tables, whole system. evidence is the single integration point.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- NOTE on dimensions: Qwen3-Embedding-8B is natively 4096-dim, but pgvector's
-- HNSW index supports at most 2000 dims. The model is matryoshka-trained, so
-- vectors are truncated to KB_EMB_DIM (1536) and re-normalized client-side.
CREATE TABLE IF NOT EXISTS evidence (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source           text NOT NULL,            -- meeting | doc | obsidian | archive | code
    source_id        text NOT NULL,            -- files: content_hash; meetings: transcript id
    unit             text NOT NULL,            -- whole | episode | chunk | page
    unit_ord         int  NOT NULL DEFAULT 0,
    raw_ref          text,                     -- path inside raw store
    content          text,                     -- distilled artifact (normalized)
    extracted_text   text,                     -- raw unit text BEFORE distillation
    embedding        vector(1536),
    tsv              tsvector GENERATED ALWAYS AS (
                         to_tsvector('russian',
                             coalesce(extracted_text, '') || ' ' || coalesce(content, ''))
                     ) STORED,
    meta             jsonb NOT NULL DEFAULT '{}',
    occurred_at      timestamptz,
    indexed_at       timestamptz NOT NULL DEFAULT now(),
    embedding_model  text,
    pipeline_version text,
    superseded_by    uuid REFERENCES evidence(id),
    UNIQUE (source, source_id, unit, unit_ord)
);

CREATE INDEX IF NOT EXISTS evidence_embedding_hnsw ON evidence
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS evidence_tsv_gin ON evidence USING gin (tsv);
CREATE INDEX IF NOT EXISTS evidence_source_idx ON evidence (source, occurred_at DESC);
CREATE INDEX IF NOT EXISTS evidence_indexed_at_idx ON evidence (indexed_at DESC);

-- blob registry: physical identity = content hash
CREATE TABLE IF NOT EXISTS raw_objects (
    content_hash text PRIMARY KEY,             -- sha256 hex
    mime         text,
    bytes        bigint,
    path         text NOT NULL,                -- raw/{hash[:2]}/{hash}/original.*
    first_seen   timestamptz NOT NULL DEFAULT now(),
    kind         text NOT NULL                 -- readable | scan | image | other
);

-- occurrences: logical identity = file within a source
CREATE TABLE IF NOT EXISTS source_objects (
    source       text NOT NULL,
    external_id  text NOT NULL,                -- drive fileId | NAS path | transcript id
    content_hash text NOT NULL REFERENCES raw_objects(content_hash),
    path         text,
    modified_at  timestamptz,
    deleted_at   timestamptz,
    PRIMARY KEY (source, external_id)
);

-- backfill/retry queue; incremental mode uses it too
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id              bigserial PRIMARY KEY,
    content_hash    text NOT NULL,
    stage           text NOT NULL DEFAULT 'normalize',  -- normalize | distill | embed
    priority        int  NOT NULL DEFAULT 0,            -- 0 incremental, 5 readable, 10 scan, 20 image
    status          text NOT NULL DEFAULT 'pending',    -- pending | processing | done | failed | skipped
    attempts        int  NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    lease_until     timestamptz,
    filter_policy   text,
    error           text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingest_jobs_claim_idx ON ingest_jobs (status, priority, id);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash text PRIMARY KEY,               -- sha256 hex of the bearer token
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS query_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    token_name text,
    tool       text NOT NULL,
    query      text,
    top_ids    jsonb
);
