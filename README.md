# RAG Backend — a template

A production-shaped backend for a Retrieval-Augmented Generation chatbot, meant to be copied and
pointed at your own documents. It takes a natural-language question, retrieves the relevant
passages from your corpus, builds a grounded prompt, and returns an answer with citations over an
authenticated REST API.

It is **API-only**. There is no UI, and consumer applications — a Next.js frontend, a Slack bot, a
mobile app — live in their own repositories and call `POST /api/query` with an API key.

```
question ──► embed ──► vector search + typo-tolerant keyword search ──► merge
                                                                         │
          answer + citations ◄── LLM ◄── grounded prompt ◄── top-K chunks ┘
```

## What you get

- **Ingestion** — a reader per format (PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML, email,
  source code, plain text as the fallback), all producing one element model, so a new format is a
  new reader and never a new chunking strategy.
- **Chunking that respects structure** — a table row stays one chunk with its column names, code
  blocks stay whole, list items keep their lead-in, prose packs by section. Sizes are counted in
  the embedding model's own tokens, with a hard cap, so nothing is silently truncated.
- **Change detection** — a run hashes raw bytes and opens only what is new, changed, deleted, or
  recorded against different settings. Deleting a file removes its chunks and vectors.
- **Retrieval** — vector similarity merged with a `pg_trgm` keyword search, so misspelled names
  still match. Configurable depth and thresholds.
- **A real API** — API-key auth, per-key rate limiting counted in Postgres (so workers share one
  allowance), one error shape, accurate OpenAPI, structured logs that cannot leak a secret.
- **Deployment** — a non-root multi-stage image, Gunicorn supervising Uvicorn workers, migrations
  as their own step, and one Compose stack per client.
- **Measurement** — a chunk quality report and a recall@k / MRR harness with a regression gate, so
  a change to chunking or retrieval is kept only if the numbers improved.

## Start here

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # fill in your provider keys
docker compose up -d            # Postgres + pgvector
python -m alembic upgrade head
python scripts/ingest.py --dry-run   # no database, no API keys, nothing stored
```

`context/get-started.md` is the full walkthrough, from migrations to your first `curl`.

## Making it yours

The template ships a deliberately tiny sample corpus so a clean clone runs immediately. Replace it
before you build anything on top:

| Replace | Why |
|---|---|
| `data/sample/` | two short documents about RAG itself, there so `--dry-run` works out of the box |
| `chunking.documents` in `config.yaml` | every document to ingest, by name — an unlisted file is not ingested |
| `evaluation/questions.yaml` | 10 cases against the sample corpus. They prove the harness runs; they measure nothing |
| `evaluation/baselines/` | ships empty on purpose — record yours once your corpus is in |
| `models.*` in `config.yaml` | the provider you have an account with, and a current model id |
| `LICENSE` | if your project is not MIT |

Then set `api.rate_limit`, `api.log_queries` and `logging.format` for how you intend to run it.

## Documentation

| Document | When |
|---|---|
| [`context/project-overview.md`](context/project-overview.md) | what this is and what it is for |
| [`context/architecture-context.md`](context/architecture-context.md) | structure, boundaries, storage model, invariants |
| [`context/design-decisions.md`](context/design-decisions.md) | why it is shaped this way, and what was deliberately not built |
| [`context/code-standards.md`](context/code-standards.md) | the conventions to implement against |
| [`context/get-started.md`](context/get-started.md) | local setup and run steps |
| [`context/chunking-guide.md`](context/chunking-guide.md) | which chunking strategy suits which content, and how to measure a change |
| [`context/adding-a-reader.md`](context/adding-a-reader.md) | supporting a new file format |
| [`context/adding-an-extractor.md`](context/adding-an-extractor.md) | supporting a new data source |
| [`context/client-onboarding.md`](context/client-onboarding.md) | standing up a new deployment end to end |
| [`deploy/README.md`](deploy/README.md) | the image, the per-deployment stack, and what must not be baked in |
| [`customisations/README.md`](customisations/README.md) | where one deployment's own code lives |
| [`docs/history/build-log.md`](docs/history/build-log.md) | how the template itself was built — history, not instruction |

## A note on shape

The architecture docs are written for **one deployment per client company**: each gets its own
database, corpus, config, secrets and keys, and isolation is physical rather than a `WHERE
client_id = ...` somebody has to remember to write. That assumption runs through `deploy/`,
`customisations/` and `client-onboarding.md`.

If you are building one product for one corpus, none of that is in your way — ignore `deploy/clients/`
and read "client" as "deployment". If you need several tenants inside one database, read
*Multi-tenancy in one deployment* in [`context/design-decisions.md`](context/design-decisions.md)
first: it is the one decision here that would be expensive to reverse.

## Requirements

Python 3.11+, PostgreSQL with pgvector, Docker Compose, and an API key for your LLM and embedding
providers. Anthropic and OpenAI are both implemented behind one interface; switching is a config
change.

## Licence

MIT — see [`LICENSE`](LICENSE).
