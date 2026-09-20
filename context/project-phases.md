# Project Phases

**Current phase: Phase 4 — Retrieval & Generation** (Phases 1, 2, and 3: COMPLETE)

Phases are ordered by dependency: each builds on the previous one, and the first working end-to-end path (ingest, then query) arrives by Phase 5. Do not start a phase until the previous phase's "Done when" criteria are met. Update the current phase marker above as work progresses.

---

## Phase 1 — Foundation — COMPLETE

**Goal:** A runnable, configurable skeleton with local infrastructure.

- Project layout: `main.py`, `config.yaml`, `extraction/`, `scripts/`, and modules for chunking, embedding, vector store, retrieval, generation, and API routes
- `requirements.txt`, `.env.example`, `.gitignore` (excluding `.env`)
- `docker-compose.yml` with Postgres and pgvector
- Typed settings loader: `config.yaml` for tunables, environment variables for secrets
- FastAPI app with a health-check route
- Tooling: ruff, mypy/pyright, pytest wired up

**Done when:** `docker-compose up -d` and `uvicorn main:app --reload` work from a clean clone, the health route responds, and config loads with validation errors on bad values.

## Phase 2 — Data Layer — COMPLETE

**Goal:** The Postgres schema and data access in place.

- Alembic set up, with migrations for `sources`, `chunks`, `api_keys`, and `query_logs`
- pgvector extension and vector storage table/index
- Repository layer for sources, chunks, and API keys
- Vector store interface, with the pgvector implementation as the default

**Done when:** `alembic upgrade head` builds the full schema from scratch, and repository and vector store operations pass tests against a real Postgres.

## Phase 3 — Ingestion Pipeline — COMPLETE

**Goal:** Turn a source into stored, searchable chunks.

- Extractor interface plus one concrete extractor for this project's first data source
- Chunker with configurable strategy, size, and overlap; chunk-level metadata preserved for traceability
- Embedding provider interface plus one implementation (configurable model)
- `scripts/ingest.py` orchestrating extract, chunk, embed, and store
- Metadata tracking: source identifier, ingestion timestamp, and SHA-256 `content_hash` per source

**Done when:** Running the ingest script on the sample source populates `sources`, `chunks`, and the vector store, and each stage passes unit tests using fake providers.

## Phase 4 — Retrieval & Generation

**Goal:** Answer a question from stored context, without the API yet.

- Question embedding and similarity search with configurable `top_k` and `similarity_threshold`
- Optional metadata filtering (source type first; date range and document type as needed)
- Fetch full chunk text from Postgres for the retrieved IDs
- LLM provider interface plus an implementation (Anthropic or OpenAI per config)
- Prompt template constraining the model to retrieved context, with the explicit "no relevant context" fallback
- Source references built from retrieved chunks

**Done when:** A function call with a question returns a grounded answer with sources, and an out-of-scope question returns the fallback instead of a fabricated answer.

## Phase 5 — API & Authentication

**Goal:** The public integration surface, testable with curl.

- `POST /api/query` with Pydantic request/response models matching `architecture-context.md`
- API key auth via `X-API-Key`, checked against hashed keys and `is_active`
- `scripts/create_api_key.py` (key shown once, stored hashed) and revocation
- Input validation and size limits on `question`
- Consistent `{ error, code }` error responses: 401, 400, 503, and 200 for the empty-retrieval fallback
- Accurate OpenAPI docs

**Done when:** The curl example in `get-started.md` works end to end, and tests cover missing key, revoked key, malformed request, and the fallback path.

## Phase 6 — Change Detection & Re-sync

**Goal:** Keep the index in step with changing sources.

- Compare current source state against stored `content_hash`
- Re-ingest only new or changed sources; mark removed sources `deleted`
- Remove stale chunks and vectors together, keeping Postgres and the vector store consistent
- Ingest script reports what was added, updated, skipped, and removed

**Done when:** Editing one source document and re-running ingestion reprocesses only that document, with no orphaned chunks or vectors left behind.

## Phase 7 — Hardening & Operations

**Goal:** Safe and reliable to run in a real environment.

- Per-key rate limiting
- Query logging (`query_logs`) and `last_used_at` updates
- Internal/admin paths for triggering ingestion and re-sync and for key management, not publicly exposed
- Timeouts and bounded retries on LLM, embedding, and vector DB calls, surfacing persistent failures as 503
- Prompt-injection safeguards: retrieved text delimited and treated as untrusted
- Structured logging that excludes secrets
- Broader test coverage, including API tests with dependency overrides

**Done when:** Rate limits and upstream failure behavior are verified by tests, and no secrets or plaintext keys appear in logs.

## Phase 8 — Extensibility & Deployment

**Goal:** Ready to reuse for a new project and to deploy.

- Additional providers behind the existing interfaces (second LLM, embedding, and vector DB option) as needed
- Documented process for adding a new extractor for a new data source
- Production `Dockerfile` (non-root) and Gunicorn with Uvicorn workers
- CORS/origin allowlist decisions documented
- `get-started.md` and the context docs updated to match what was built

**Done when:** The image builds and runs against a production-style config, and swapping to a new data source requires changes only in the extraction layer and config.
