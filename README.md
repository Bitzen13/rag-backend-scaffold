# RAG Backend

[![CI](https://github.com/Bitzen13/rag-backend-scaffold/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Bitzen13/rag-backend-scaffold/actions/workflows/ci.yml)

An API-only backend for a custom Retrieval Augmented Generation system (RAG). It ingests a folder of files,
stores them as searchable chunks in Postgres, and answers a question over a REST API. The model
answers using only the passages it retrieved, and every answer cites its sources.

**Stack:** Python 3.11+, FastAPI, PostgreSQL with pgvector, Alembic, Docker. Anthropic or OpenAI
for the model, and OpenAI for embeddings. Providers are chosen in `config.yaml`.

---

## How it works

```
documents ─► read ─► chunk ─► embed ─► Postgres + pgvector
                                              │
question ──► embed ─► vector search + keyword search ─► prompt ─► LLM ─► output checks ─► answer + sources
```

- **Ingestion.** There's a reader per format: PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML,
  email and source code, with plain text for anything else. Every reader produces the same
  element model, so chunking never depends on the file format.
- **Chunking.** A table row stays one chunk with its column names. Code blocks stay whole. A list
  item keeps the line that introduces it, and prose is packed by section. Sizes are counted in
  the embedding model's own tokens, with a hard cap, so the embedding model never silently
  truncates a chunk.
- **Change detection.** A run hashes each file's bytes and opens only what is new, changed or
  removed. Deleting a file deletes its chunks.
- **Retrieval.** Vector similarity is merged with typo-tolerant keyword search (`pg_trgm`), so
  exact names and misspellings still match.
- **API.** One endpoint, `POST /api/query`, with API-key auth, per-key rate limiting, one error
  shape and accurate OpenAPI docs. Ingestion and key management sit on internal admin routes
  that aren't mounted unless an admin token is set.

## Quick start

```bash
cd backend                              # every command below runs from here
pip install -r requirements-dev.txt
cp .env.example .env                    # add your OPENAI_API_KEY and ANTHROPIC_API_KEY
docker compose --env-file .env up -d    # Postgres + pgvector on localhost (or use your own, below)
python -m alembic upgrade head          # create the schema

# 1. Put your files in data/documents/ (file types and chunking are detected automatically)
python scripts/ingest.py --dry-run      # preview the chunks (no database, no API keys)
python scripts/ingest.py                # extract, chunk, embed and store

# 2. Serve it
python scripts/create_api_key.py --label "my-frontend"   # prints the key once
uvicorn app.main:app --reload

curl -X POST http://localhost:8000/api/query \
  -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  -d '{"question": "What does the warranty cover?"}'
```

Interactive API docs are at `http://localhost:8000/docs`. `python scripts/ask.py "question"` asks
from the command line, without the API.

**Using your own database.** Skip the `docker compose` step and set `DATABASE_URL` in `.env` to any
PostgreSQL that has pgvector 0.5 or later available. Connection strings from hosted providers work
as they are, including `?sslmode=require`. The migrations enable the `vector` and `pg_trgm`
extensions, so the role running them needs permission to do that, or an admin can enable both
first.

## Safety

Anyone who can add a document can write instructions aimed at the model. The model has no tools,
so the defences limit what a successful attack can achieve:

- **Output checks.** Images and links not on an allow-list are removed from answers, excerpts and
  source names, so an answer can't make the user's browser send data anywhere.
- **The prompt can't be forged.** Document text and file names are escaped, so they can't break
  out of the prompt section they belong to.
- **File limits.** Oversized files, zip bombs, huge spreadsheets, YAML alias bombs and symlinks
  out of the folder are each refused with a named reason, and the rest of the run carries on.
- **Mass-deletion guard.** A run that would delete more than half the corpus stops before doing
  anything.

Each decision, and the tests that prove it, is in
**[backend/docs/safety-considerations.md](backend/docs/safety-considerations.md)**.

## Metrics

**Quality gates.** CI runs these on every push, on Python 3.11 and 3.12:

| Check | Result |
|---|---|
| Tests | 672 tests, about 90 of them against a real Postgres |
| Types | `mypy --strict`, no errors |
| Lint and format | `ruff`, clean |
| Production image | builds, and runs as a non-root user |

**Safety.** 11 attack documents run in CI against a fake model that obeys every instruction it
finds, which is the worst case. Each CI run's Summary page shows the full table.

| Attack goal | Before the safety work | Now |
|---|---|---|
| Exfiltrate data via an image or link | 5 / 5 succeeded | **0 / 5** |
| Inject a `javascript:` or `data:` URL | 2 / 2 | **0 / 2** |
| Break the prompt's structure | 1 / 2 | **0 / 2** |
| Make the model say something (canary) | 4 / 4 | 4 / 4 (only the model can refuse these) |

**Measured costs that shaped the design:**

| Case | Without the defence |
|---|---|
| 264-byte YAML file with nested aliases | 2.3 million characters to embed |
| 4.8 KB spreadsheet declaring a 4,000 × 4,000 grid | 135 MB of memory |
| Re-sync of a 40 MB corpus: hash every file vs parse every file | 0.03 s vs 69 s, so only changed files are parsed |

**Retrieval quality.** These tools measure your own corpus. It ships empty, so there are no
numbers here yet:

- `python scripts/ingest.py --dry-run` reports chunk quality: size distribution, oversized,
  empty or duplicate chunks, and chunks cut mid-sentence.
- `python scripts/evaluate.py` measures **recall@k** and **MRR** over the questions in
  `app/evaluation/questions.yaml`. `--save-baseline` / `--baseline` fail a change that makes them
  worse.

## Design decisions

- **The model gets no tools and no network access.** At worst, a prompt injection changes an
  answer. It can't take an action. Giving the model tools would mean rethinking the safety design
  first.
- **pgvector, not a separate vector database.** The vectors live next to the chunks they belong
  to, so one transaction keeps them consistent and there's one less service to run. A hosted
  vector database becomes worth it only at a scale where pgvector's index stops keeping up.
- **One database per deployment, not a tenant column.** Customers stay separate because they're
  in different databases, not because every query remembers a `WHERE tenant_id = …`.
- **Two hashes for change detection.** A hash of the raw bytes decides whether a file is opened at
  all. A hash of the extracted text decides whether it's re-embedded. mtime and file size are
  never trusted: a silently stale chunk is a wrong answer.
- **Shared state lives in Postgres.** Rate-limit counts and the one-ingestion-at-a-time lock are
  atomic statements in the database, so several workers share one limit instead of each having
  their own.
- **A setting that can't take effect fails at start-up.** An unknown provider, an overlap larger
  than the chunk size, or a chunk bigger than the embedding model accepts is rejected when the
  config loads, not discovered later as bad answers.
- **Secrets never reach a log.** Redaction happens in the log formatter, so it also covers library
  logs and stack traces.
- **Changes to chunking and retrieval are measured.** Keep a change only if recall@k or MRR
  improved.

## Project layout

```
docker-compose.yml     local Postgres + pgvector
backend/
  app/                 the application
    api/               FastAPI routes, auth, rate limiting, error shape
    extraction/        readers per file format, file limits
    chunking/          fixed-size and document-aware chunkers
    embedding/         embedding providers
    vector_store/      pgvector storage and search
    retrieval/         vector + keyword search, merged
    generation/        prompt template and LLM providers
    safety/            output checks
    ingestion/         the ingest pipeline and change detection
    services/          wiring used by the API and the scripts
    evaluation/        retrieval metrics, the evaluation set, the attack set
    db/                models and repositories
    main.py            app entry point
    settings.py        typed config (config.yaml) and secrets (.env)
  migrations/          Alembic schema migrations
  scripts/             ingest, ask, evaluate, API keys, safety report
  tests/               pytest suite
  docs/                safety considerations
  data/documents/      your documents go here
  docker/              Gunicorn config and database init
  Dockerfile           production image, built from the repo root
  config.yaml          every tunable setting
```

## License

MIT, see [LICENSE](backend/LICENSE).
