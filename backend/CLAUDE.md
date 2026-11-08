# CLAUDE.md

API-only RAG backend. Read `../README.md` for what it does and why, and
`docs/safety-considerations.md` before changing anything that touches documents, prompts or
answers.

## Stack and layout

Python 3.11+, FastAPI, PostgreSQL + pgvector, Alembic, Docker Compose. All application code is in
`app/` (imports read `from app.x import ...`). Tunables are in `config.yaml`; secrets come only
from environment variables (`.env`). No UI code.

## Commands

Run from `backend/`; `docker-compose.yml`, `README.md` and the ignore files are one level up.

```bash
docker compose --env-file .env up -d && python -m alembic upgrade head   # local database
ruff check . && ruff format --check . && mypy . && pytest   # must all pass before finishing
python scripts/ingest.py --dry-run                         # chunk preview, no DB or keys
```

## Rules

- Each pipeline stage (extraction, chunking, embedding, vector store, retrieval, generation) sits
  behind its own interface; change stages independently.
- Schema changes are new Alembic migrations, never edits to applied ones.
- A reader that changes what it extracts bumps its `version`.
- Everything derived from a document is untrusted: it goes through `app/safety/output.py` before
  reaching a client, and file cost is bounded by `extraction.limits`.
- Tests never call a real LLM or embedding API. Tests that prove a safety decision carry its
  `safety_*` marker (see `pyproject.toml`).
- Keep responses brief. If a change alters a documented decision, update the README or
  `docs/safety-considerations.md` in the same change.
