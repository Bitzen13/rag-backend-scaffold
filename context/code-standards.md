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

## Extraction & Chunking

- Readers describe a file as `DocumentElement`s and make no chunking decisions. Chunking rules are written against element kinds, never against a file format — if a rule seems to need the format, the element model is missing a field or a kind. See `adding-a-reader.md`.
- A reader never assumes an encoding (`decode_bytes`), always normalises newlines before line-based parsing, and always strips characters Postgres cannot store. A file it cannot read raises `ReaderError`, is logged, and the run continues — one bad file must not stop ingestion.
- An optional reader dependency is imported through `require()`, so a missing package names itself instead of failing at start-up. An unknown extension falls back to plain text rather than being skipped.
- Element offsets are assigned in one place (`extraction/elements.ElementList.build`). Never assign `char_start`/`char_end` by hand: they are what makes a retrieved chunk traceable to its source.
- Chunk sizes are token counts from the configured embedding model's own tokenizer, never word counts. Every chunk passes through `ChunkAssembler`, which counts it exactly and enforces the hard cap, so no chunk can be silently truncated by the provider.
- A chunk's context (heading path, table caption, list lead-in, question) is counted against its budget, not added on top of it.
- Structure detection must be conservative where a false positive corrupts content. Borderless-table detection is the worked example: it requires several independent signals, because reading prose as table rows is worse than missing a table.
- Changing chunking or retrieval behaviour is a measurement, not an opinion: record a baseline with `scripts/ingest.py --dry-run --qa-json ...` and `scripts/evaluate.py --save-baseline ...` first, and keep the change only if recall@k or MRR improved, or it fixed a defect without regressing them.

## Configuration & Secrets

- All tunable parameters (chunk size/overlap, `top_k`, similarity threshold, model and provider choices) live in `config.yaml` and are loaded once into a typed settings object (e.g. Pydantic settings). No magic numbers in pipeline code.
- A setting that cannot take effect is a validation error, not a silent no-op: `prose_splitting` on `fixed_size`, a `chunk_overlap` at or above its `chunk_size`, a `chunk_size` above the hard cap, a hard cap above the embedding model's input limit.
- Secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DATABASE_URL`, `VECTOR_DB_URL`) come from environment variables only. Never hardcode, log, or commit them; keep `.env` out of version control and maintain `.env.example`.

## Database & Migrations

- PostgreSQL schema changes go through Alembic migrations. Never alter the schema by hand or from application code.
- Use parameterized queries or the ORM. Never build SQL by string concatenation.
- Keep data access in a repository/data layer; route handlers and pipeline stages do not write raw queries.
- Ingestion writes to Postgres and the vector DB must stay consistent: when a source is re-synced or deleted, remove its stale chunks and vectors in the same operation flow, and keep `chunks.id` matching the vector DB `chunk_id`.
- Change detection uses two SHA-256 hashes, and they are not interchangeable: `sources.source_hash` (of the raw bytes) decides whether a source is opened at all, and `sources.content_hash` (of the extracted text) decides whether it is re-chunked and re-embedded. Never use mtime or size in their place — a silently stale chunk is a wrong answer, which is worse than a wasted parse.
- Anything that can be decided without extraction must be decided without it: a run enumerates candidates, loads the stored state for the source type in one query, and only then opens what actually changed. Extraction is the expensive part of a run by two or three orders of magnitude.
- A reader that changes what it extracts must have its `version` bumped. The file's bytes do not change when a reader improves, so without that bump the improvement never reaches documents that are already ingested.

## API Routes

- Validate and parse request input before any logic runs. Enforce size limits on `question` before it reaches the embedding or LLM call.
- Enforce API key auth before any read or mutation. Validate `X-API-Key` against `api_keys.key_hash`, reject inactive keys, and never store or log plaintext keys. Compare hashes in constant time.
- Apply per-key rate limiting on public routes.
- Internal/admin routes (ingestion, re-sync, key issuance/revocation) are never publicly exposed and require their own protection.
- Return consistent, predictable response shapes. Errors return `{ "error": string, "code": string }` with appropriate HTTP status codes: 401 invalid/missing key, 400 malformed request (map FastAPI's default 422 validation errors to this), 503 upstream unreachable. An empty retrieval result is a normal 200 (the LLM is still called, with an empty context, and no sources are returned), not an error.
- Keep route handlers thin — push logic into shared modules (retrieval, generation, services).

## Retrieval & Generation

- The prompt template must let the assistant chat naturally (greetings, small talk, questions about itself) while requiring every factual answer to come only from the retrieved context, and telling it to say it lacks the information rather than answer from its own knowledge. No canned answer strings are hard-coded. Do not weaken the grounding rule to make answers look fuller.
- Apply the similarity threshold from config before building the prompt. If nothing passes, still call the LLM but with an explicit "no relevant passages were found" context, never a pretend-grounded one, and return no sources.
- Every answer returns source references (`chunk_id`, `source_uri`, `excerpt`) traceable to stored chunks.
- Treat retrieved chunk text as untrusted data inside the prompt. Delimit it clearly so document content cannot act as instructions to the model.
- Wrap LLM, embedding, and vector DB calls with timeouts and bounded retries. Surface persistent failures as 503, not as silent empty answers.

## Testing

- Use `pytest` (with `pytest-asyncio` or `anyio` for async code).
- Each pipeline stage is testable in isolation using fake or stub providers — tests must not call real LLM or embedding APIs.
- Test the API with FastAPI's `TestClient`/`httpx.AsyncClient` and dependency overrides for auth, database, and providers.
- Cover the key behaviors: auth failure, revoked key, malformed request, empty retrieval fallback, and change detection on re-sync.
- Chunking rules are tested against hand-built elements, not against files, because the rules belong to the element model. Readers are tested against small real files of their format, and must cover what breaks real corpora: an empty file, one huge paragraph, a non-UTF-8 encoding, CRLF line endings, Unicode and CJK text, and a file that is not the format its extension claims.

## Containers

- Keep the Docker image minimal and run as a non-root user.
- `docker-compose` is for local development only (Postgres and pgvector). Production runs Gunicorn with Uvicorn workers.
