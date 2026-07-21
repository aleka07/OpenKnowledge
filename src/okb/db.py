import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import settings


def connect() -> psycopg.Connection:
    conn = psycopg.connect(settings.db_dsn, row_factory=dict_row, autocommit=True)
    register_vector(conn)
    return conn


def init_db(conn: psycopg.Connection, migrations_dir) -> list[str]:
    applied = []
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        conn.execute(sql_file.read_text())
        applied.append(sql_file.name)
    return applied


CLAIM_SQL = """
UPDATE ingest_jobs SET
    status = 'processing', locked_by = %(worker)s,
    lease_until = now() + interval '10 minutes',
    attempts = attempts + 1, updated_at = now()
WHERE id = (
    SELECT id FROM ingest_jobs
    WHERE (status = 'pending' AND next_attempt_at <= now())
       OR (status = 'processing' AND lease_until < now())
    ORDER BY priority, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
"""


def claim_job(conn: psycopg.Connection, worker: str) -> dict | None:
    return conn.execute(CLAIM_SQL, {"worker": worker}).fetchone()


def finish_job(conn: psycopg.Connection, job_id: int, status: str, error: str | None = None) -> None:
    # failed claims get retried with linear backoff until MAX_ATTEMPTS (checked by caller)
    conn.execute(
        """UPDATE ingest_jobs SET status = %s, error = %s, locked_by = NULL,
           lease_until = NULL, next_attempt_at = now() + interval '1 minute' * attempts,
           updated_at = now() WHERE id = %s""",
        (status, error, job_id),
    )
