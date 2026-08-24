# Tests

Unit tests (no DB, no LLM, run anywhere):

    uv run pytest

Full suite including live-DB invariants (run on gx10-1):

    KB_IT=1 uv run pytest

The integration tests are the manual post-batch checks turned into code:
per-document cap in search, project scoping, passthrough share below 5%,
no orphan embeddings, `okb reproject` idempotency.
