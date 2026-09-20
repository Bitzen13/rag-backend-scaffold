# Code Standards

## General

- Keep modules small and single-purpose.
- Fix root causes — do not layer workarounds.
- Do not mix unrelated concerns in one module or route.
- Respect the system boundaries defined in `architecture-context.md`.
- Each pipeline stage (extraction, chunking, embedding, vector storage, retrieval, generation) is an independent, swappable component. Stages communicate through defined interfaces, never by reaching into each other's internals.

## Python

- Target Python 3.11+.
- Type-annotate all function signatures and public attributes. Run a static type checker (mypy or pyright) in strict mode.
- Avoid `Any`; use explicit types, `TypedDict`, dataclasses, or Pydantic models.
- Validate unknown external input at system boundaries before trusting it — this applies to `/api/query` payloads, extracted source content, and responses from LLM, embedding, and vector DB providers.
- Use `typing.Protocol` or an abstract base class for swappable component contracts (LLM provider, embedding provider, vector store, extractor, chunker). Concrete implementations are selected from `config.yaml`, not hardcoded.
- Follow PEP 8. Format and lint with a single tool (ruff) so style is not debated in review.
- Use `pathlib` for paths, `logging` (not `print`) for diagnostics, and specific exceptions rather than bare `except`.

## FastAPI

- Use `async def` for route handlers and for any I/O (database, vector DB, LLM, embedding calls). Never call blocking I/O inside an async handler; if a library is sync-only, run it in a threadpool.
- Define request and response bodies as Pydantic models. The `POST /api/query` shape in `architecture-context.md` is a contract with external consumers — changes must be versioned or backward-compatible.
- Use dependency injection (`Depends`) for auth, database sessions, config, and provider clients so they can be overridden in tests.
- Organize routes with `APIRouter`, one router per concern. Keep `main.py` limited to app construction and router registration.
- Keep OpenAPI output accurate: set `response_model`, declare error responses, and add summaries so consumers can generate clients without reading source.
- Long-running work belongs outside request handlers. Ingestion, re-sync, and bulk re-embedding run through the CLI/scripts or the internal admin path, never inline in `/api/query`.

## Configuration & Secrets

- All tunable parameters (chunk size/overlap, `top_k`, similarity threshold, model and provider choices) live in `config.yaml` and are loaded once into a typed settings object (e.g. Pydantic settings). No magic numbers in pipeline code.
- Secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL`, `VECTOR_DB_URL`) come from environment variables only. Never hardcode, log, or commit them; keep `.env` out of version control and maintain `.env.example`.

## Database & Migrations

- PostgreSQL schema changes go through Alembic migrations. Never alter the schema by hand or from application code.
- Use parameterized queries or the ORM. Never build SQL by string concatenation.
- Keep data access in a repository/data layer; route handlers and pipeline stages do not write raw queries.
- Ingestion writes to Postgres and the vector DB must stay consistent: when a source is re-synced or deleted, remove its stale chunks and vectors in the same operation flow, and keep `chunks.id` matching the vector DB `chunk_id`.
- Use `content_hash` (SHA-256) for change detection; re-ingest only new or changed sources.

## API Routes

- Validate and parse request input before any logic runs. Enforce size limits on `question` before it reaches the embedding or LLM call.
- Enforce API key auth before any read or mutation. Validate `X-API-Key` against `api_keys.key_hash`, reject inactive keys, and never store or log plaintext keys. Compare hashes in constant time.
- Apply per-key rate limiting on public routes.
- Internal/admin routes (ingestion, re-sync, key issuance/revocation) are never publicly exposed and require their own protection.
- Return consistent, predictable response shapes. Errors return `{ "error": string, "code": string }` with appropriate HTTP status codes: 401 invalid/missing key, 400 malformed request (map FastAPI's default 422 validation errors to this), 503 upstream unreachable. An empty retrieval result is a 200 with the "no relevant context" answer, not an error.
- Keep route handlers thin — push logic into shared modules (retrieval, generation, services).

## Retrieval & Generation

- The prompt template must constrain the LLM to answer only from retrieved context and include the explicit "no relevant context" fallback. Do not weaken this to make answers look fuller.
- Apply the similarity threshold from config before building the prompt. If nothing passes, do not call the LLM with an empty context as if it were grounded.
- Every answer returns source references (`chunk_id`, `source_uri`, `excerpt`) traceable to stored chunks.
- Treat retrieved chunk text as untrusted data inside the prompt. Delimit it clearly so document content cannot act as instructions to the model.
- Wrap LLM, embedding, and vector DB calls with timeouts and bounded retries. Surface persistent failures as 503, not as silent empty answers.

## Testing

- Use `pytest` (with `pytest-asyncio` or `anyio` for async code).
- Each pipeline stage is testable in isolation using fake or stub providers — tests must not call real LLM or embedding APIs.
- Test the API with FastAPI's `TestClient`/`httpx.AsyncClient` and dependency overrides for auth, database, and providers.
- Cover the key behaviors: auth failure, revoked key, malformed request, empty retrieval fallback, and change detection on re-sync.

## Containers

- Keep the Docker image minimal and run as a non-root user.
- `docker-compose` is for local development only (Postgres and pgvector). Production runs Gunicorn with Uvicorn workers.
