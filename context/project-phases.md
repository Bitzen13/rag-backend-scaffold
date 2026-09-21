# Project Phases

**Current phase: Phase 5 — Adaptive Document-Aware Chunking** (Phases 1–4: COMPLETE)

Phases are ordered by dependency: each builds on the previous one, and the first working end-to-end path (ingest, then query) already exists as scripts (`scripts/ingest.py`, `scripts/ask.py`); the public API arrives in Phase 7. Do not start a phase until the previous phase's "Done when" criteria are met. Update the current phase marker above as work progresses.

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

## Phase 4 — Retrieval & Generation — COMPLETE

**Goal:** Answer a question from stored context, without the API yet.

- Question embedding and similarity search with configurable `top_k` and `similarity_threshold`
- Optional metadata filtering (source type first; date range and document type as needed)
- Fetch full chunk text from Postgres for the retrieved IDs
- LLM provider interface plus an implementation (Anthropic or OpenAI per config)
- Conversational prompt template: natural replies to small talk, factual answers only from retrieved context, no hard-coded fallback text
- Source references built from retrieved chunks

**Done when:** A function call with a question returns a grounded answer with sources, and an out-of-scope question gets a "not in my knowledge base" reply instead of a fabricated answer.

## Phase 5 — Adaptive Document-Aware Chunking

**Goal:** One adaptive chunking strategy that handles any kind of document, reached by adding readers for more formats and better prose splitting, and by measuring real retrieval quality instead of adding strategy after strategy.

**Design principle.** The chunker (`document_aware`) works on an abstract element model: headings, paragraphs, and table rows. What differs between a PDF, Markdown, HTML, a spreadsheet, or an email is the reader that produces those elements, not the chunking algorithm. So new formats are new readers emitting the same elements, and there is no separate strategy per format. `fixed_size` stays for plain text. Strategy choice stays manual and per document in `chunking.documents`; nothing is picked automatically.

Already built (Phases 3 and 4): `fixed_size` (word windows); `document_aware` with a structure-preserving PDF reader (headings ranked by the document's own typography, table rows with headers carried across pages, captions, columns, paragraphs joined across pages).

**1. Measure first**
- Retrieval evaluation harness: a small file of question-to-expected-chunk (or expected-passage) pairs, reporting recall@k and MRR for a document or corpus. Run it with the full retrieval path (vector plus fuzzy keyword search).
- Chunk QA report in `--dry-run`: chunk counts, size distribution, oversized or undersized chunks, empty chunks, exact duplicates, and warnings for suspicious cuts (a chunk that starts mid-sentence, ends mid-word, or is mostly a URL).
- Record a baseline before any other item below. Keep a change only if it improves recall@k or fixes a defect without regressing it.

**2. More readers, one element model**
- Formats: Markdown, HTML (sections, lists, tables, with boilerplate removed), DOCX, CSV and Excel, JSON and YAML, source code, and email (one element per message). Plain `.txt` gets light structure detection (headings, lists, paragraphs).
- Extend the element model where a format needs it: list items, code blocks, Q&A pairs, speaker turns (transcripts), slides. Each element carries its context (heading path, page, slide, line range, sender or speaker).
- Chunk rules per element kind: a code block or table stays whole; list items are grouped under their lead-in; a question stays with its answer; transcript turns are grouped by speaker and time window.
- PDF reader gaps to close: borderless tables, checking multi-column reading order and running-header removal on real documents, and an optional OCR fallback for scanned pages.
- Corpus-specific cleanup stays in config (regular expressions), separate from structure detection.

**3. Better prose splitting inside `document_aware`**
- Oversized paragraphs split at sentence boundaries (never mid-sentence), with sentence-level overlap, using multilingual-safe sentence segmentation.
- Optional topic-boundary (semantic) splitting for long unstructured prose: embed sentences and cut where similarity between neighbours drops, with minimum and maximum chunk size. It is a setting of the same strategy (`prose_splitting: paragraph | sentence | semantic`), off unless the harness shows it helps.

**4. Sizing and per-document options**
- Token-accurate sizing (a real tokenizer instead of word counts), with a hard cap under the embedding model's input limit so no chunk is ever silently truncated.
- Per-document options in config (for example `chunk_size`, `chunk_overlap`, `prose_splitting`) instead of one global setting, with validation.

**5. Optional retrieval-quality extras (adopt only if the harness shows a gain)**
- `parent_child`: retrieve on small chunks but pass the surrounding section to the LLM (small-to-big); needs a parent reference stored with each chunk.
- `contextual`: an LLM writes a one-line note on where each chunk sits in the document, prepended before embedding; batched and cached by content hash, with concurrency and budget caps.
- Not planned: `propositions` (LLM-rewriting text into atomic facts), because of its per-passage LLM cost.

**6. Groundwork for Phase 6**
- Record the strategy and its parameters with each ingested source, so Phase 6 can detect a configuration change and re-chunk the document.

**Testing and docs**
- Tests use small hand-built element fixtures and real sample files supplied by the user, with fake embedding and LLM providers (no real API calls). Cover empty input, a single huge paragraph, Unicode, and very short documents.
- A decision guide (which strategy and options for which content type) and a short guide to adding a new reader.

**Done when:** The evaluation harness and QA report exist and a baseline is recorded; the readers listed above are implemented and tested; sentence-aware splitting works and is selectable per document; sizing is token-accurate with a hard cap; each source records the strategy and parameters it was chunked with; and every optional extra that was adopted has a measured improvement on the harness.

## Phase 6 — Change Detection & Re-sync

**Goal:** Keep the index in step with changing sources.

- Compare current source state against stored `content_hash`
- Also compare the chunking strategy and parameters recorded with each source (from Phase 5) against `config.yaml`, so changing a document's strategy, `chunk_size`, or `chunk_overlap` re-chunks and re-embeds it on the next run, instead of being skipped as unchanged
- Re-ingest only new or changed sources; mark removed sources `deleted`
- Remove stale chunks and vectors together, keeping Postgres and the vector store consistent
- Ingest script reports what was added, updated, skipped, and removed

**Done when:** Editing one source document and re-running ingestion reprocesses only that document, with no orphaned chunks or vectors left behind, and changing a document's chunking strategy in config re-chunks it on the next run.

## Phase 7 — API & Authentication

**Goal:** The public integration surface, testable with curl.

- `POST /api/query` with Pydantic request/response models matching `architecture-context.md`
- API key auth via `X-API-Key`, checked against hashed keys and `is_active`
- `scripts/create_api_key.py` (key shown once, stored hashed) and revocation
- Input validation and size limits on `question`
- Consistent `{ error, code }` error responses: 401, 400, 503, and 200 (with an empty `sources` list) when retrieval finds nothing
- Accurate OpenAPI docs

**Done when:** The curl example in `get-started.md` works end to end, and tests cover missing key, revoked key, malformed request, and the empty-retrieval path.

## Phase 8 — Hardening & Operations

**Goal:** Safe and reliable to run in a real environment.

- Per-key rate limiting
- Query logging (`query_logs`) and `last_used_at` updates
- Internal/admin paths for triggering ingestion and re-sync and for key management, not publicly exposed
- Timeouts and bounded retries on LLM, embedding, and vector DB calls, surfacing persistent failures as 503
- Prompt-injection safeguards: retrieved text delimited and treated as untrusted
- Structured logging that excludes secrets
- Broader test coverage, including API tests with dependency overrides

**Done when:** Rate limits and upstream failure behavior are verified by tests, and no secrets or plaintext keys appear in logs.

## Phase 9 — Extensibility & Deployment

**Goal:** Ready to reuse for a new project and to deploy.

- Additional providers behind the existing interfaces (second LLM, embedding, and vector DB option) as needed
- Documented process for adding a new extractor for a new data source
- Production `Dockerfile` (non-root) and Gunicorn with Uvicorn workers
- CORS/origin allowlist decisions documented
- `get-started.md` and the context docs updated to match what was built

**Done when:** The image builds and runs against a production-style config, and swapping to a new data source requires changes only in the extraction layer and config.
