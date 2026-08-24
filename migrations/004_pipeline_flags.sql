-- Operator flags for the pipeline (e.g. pause). One row per flag.
CREATE TABLE IF NOT EXISTS pipeline_flags (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
