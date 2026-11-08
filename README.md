# RAG Backend

[![CI](https://github.com/Bitzen13/rag-backend-scaffold/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Bitzen13/rag-backend-scaffold/actions/workflows/ci.yml)

## Overview

**Prompt-injection resistant, CI-tested.**

An API-only backend for a custom Retrieval Augmented Generation (RAG) system. It ingests
`backend/data/documents`, stores searchable chunks in Postgres, and answers questions over a REST
API. The model answers only from retrieved passages, and each API response lists its sources.

**Stack:** Python 3.11+, FastAPI, PostgreSQL with pgvector, Alembic, Docker. Anthropic or OpenAI
for the model, OpenAI for embeddings.

**Safety:** Documents are treated as untrusted. The model has no tools or network access, and
output filtering, prompt escaping and file limits still hold if a document hijacks it.

## Quick start

```bash
cd backend
pip install -r requirements-dev.txt
cp .env.example .env                    # add OPENAI_API_KEY and ANTHROPIC_API_KEY
docker compose --env-file .env up -d    # or use your own database, below
python -m alembic upgrade head

# Add files to data/documents/, then:
python scripts/ingest.py                # --dry-run previews chunks, no database or keys
python scripts/create_api_key.py --label local   # prints the key once
uvicorn app.main:app --reload
```

Try it at `http://localhost:8000/docs`, or skip the API with `python scripts/ask.py "question"`.

**Your own database:** skip the `docker compose` step and set `DATABASE_URL` to any PostgreSQL with
pgvector 0.5+. Hosted connection strings work as they are. The migrations enable `vector` and
`pg_trgm`, so the role running them needs permission to do that.

## How it works

```
documents ─► read ─► chunk ─► embed ─► Postgres + pgvector
                                              │
question ──► embed ─► vector search + keyword search ─► prompt ─► LLM ─► output checks ─► answer + sources
```

- **Read:** file types are detected automatically: PDF, Markdown, HTML, DOCX, CSV, Excel, JSON,
  YAML, email, code and plain text.
- **Chunk:** tables, code, lists and sections stay intact, within the embedding model's token limit.
- **Sync:** only new or changed files are processed. Deleting a file deletes its chunks.
- **Search:** vector similarity plus typo-tolerant keyword search.
- **Serve:** `POST /api/query`, with API-key auth and per-key rate limits.

## Safety

**Threat model:** Anyone who can add a document controls text the model reads. Injection filters
eventually fail, so this design assumes injection succeeds and limits what it can achieve. The
filters, escaping and limits are ordinary application code that runs before the model sees the
text or after it replies. The model can't call, change or skip that code.

### Controls

| Risk | Control | Why |
|---|---|---|
| **The model takes an action** | No tools or network access. | A hijacked model can only produce text. |
| **Data leaves through the answer** | All images are removed. Links are removed unless their host is allow-listed (empty by default). Only `http` and `https` can pass. | An image fetches its URL on render, with no click. |
| **The output filter is bypassed** | Handles inline and reference-style Markdown, raw HTML, autolinks and bare URLs. Decodes URLs as a browser would. | Reference-style links beat the filter in EchoLeak. |
| **Unfiltered text reaches a client** | The same check covers answers, excerpts and source names. | Excerpts and file names never pass through the model. |
| **A document forges the prompt** | Document text, file names and the question are escaped. | `</context>` stays inert data. This protects structure, not model behaviour. |
| **The model obeys a document** | The system prompt declares context untrusted. | Best effort. No other control depends on it. |
| **One file exhausts resources** | Limits on file size, archive size, spreadsheet cells, extracted text, chunks and metadata. YAML aliases aren't expanded. | Declared sizes are checked before expansion. The file is refused and the run continues. |
| **A file escapes the corpus** | Symlinks resolving outside the document folder are refused. | Checked before any byte is read. |
| **A bad run wipes the corpus** | A run deleting over 50% of sources stops. `--allow-mass-delete` overrides one run. | An unmounted folder looks like every file was deleted. |
| **A client renders hostile text** | `answer`, `excerpt` and `source_uri` are documented as untrusted. | Only the client can escape for its own rendering context. |
| **Credentials leak** | API keys are stored as SHA-256 hashes and compared in constant time. Secrets come only from the environment. Logs are redacted. | Redaction sits in the formatter, so library logs and tracebacks are covered. |
| **The API is abused** | Per-key rate limits, a question length cap and statement timeouts. CORS is off by default. | Limits are counted in Postgres, so workers share one allowance. |
| **Admin routes are exposed** | Not mounted unless an admin token is set. | A route that doesn't exist can't be attacked. |
| **The deployment is too open** | Non-root container with read-only `/app`. Local Postgres binds to loopback. Documents and secrets stay out of the image. | The local database password is public. |

### Verification

- 11 attack documents run in CI against a fake model that obeys every instruction it finds.
- All 9 leak and prompt-forgery attempts fail.
- Each decision has a pytest marker, and each CI run publishes a per-decision evidence table.

### Residual risk

- A document can still make the model say something false. Only a real model can refuse that.
- Every supported file in the document folder is ingested. Keep only searchable content there.

## Metrics

CI runs on every push, on Python 3.11 and 3.12:

| Check | Result |
|---|---|
| Tests | 672 tests, about 90 of them against a real Postgres |
| Types | `mypy --strict`, no errors |
| Lint and format | `ruff`, clean |
| Production image | builds, and runs as a non-root user |

**Attack set:**

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
| Re-sync of a 40 MB corpus: hash every file vs parse every file | 0.03 s vs 69 s |

**Retrieval quality** is measured on your own corpus:

- `python scripts/ingest.py --dry-run` reports chunk size distribution, oversized, empty or
  duplicate chunks, and chunks cut mid-sentence.
- `python scripts/evaluate.py` measures **recall@k** and **MRR** over
  `app/evaluation/questions.yaml`. `--save-baseline` / `--baseline` fail a change that makes them
  worse.

## Design decisions

- **pgvector, not a separate vector database.** Vectors live next to their chunks, so one
  transaction keeps them consistent and there's one less service to run.
- **One database per deployment, not a tenant column.** Customers are separated by database, not
  by every query remembering a `WHERE tenant_id = …`.
- **Two hashes for change detection.** A raw-bytes hash decides whether a file is opened. An
  extracted-text hash decides whether it's re-embedded. mtime and file size are never trusted.
- **Shared state lives in Postgres.** Rate limits and the ingestion lock are atomic database
  statements, so all workers share them.
- **A setting that can't take effect fails at start-up.** An unknown provider, an overlap larger
  than the chunk size, or a chunk too big for the embedding model is rejected when config loads.
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
