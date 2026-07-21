# OpenKnowledge

Team knowledge base over work sources: one `evidence` table in Postgres+pgvector,
MCP tools out. Design docs: [docs/](docs/).

## Layout

- `src/okb/` — ingest pipeline (inventory → normalize → distill → embed → upsert),
  hybrid retrieval (RRF + freshness decay), MCP server (5 tools, Bearer auth).
- `migrations/` — plain SQL, applied in filename order by `okb init-db`.
- Inference: vLLM endpoints on gx10-2 (generation Qwen3.6-35B, embedding Qwen3-Emb-8B
  truncated to 1536 dims — pgvector HNSW caps at 2000, the model is matryoshka-trained).

## Quick start (on gx10-1)

```bash
uv sync
cp .env.example .env            # adjust endpoints/DSN if needed
docker compose up -d            # Postgres 16 + pgvector on 127.0.0.1:15432
uv run okb init-db
uv run okb ingest ~/kb-data/inbox
uv run okb worker --once
uv run okb token-create <name>  # prints the bearer token once
uv run okb mcp                  # streamable HTTP on :17000/mcp
```

Client hookup:

```bash
claude mcp add openknowledge --transport http http://<kb-host>:17000/mcp \
  --header "Authorization: Bearer kb_live_..."
```
