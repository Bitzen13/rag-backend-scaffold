# Project Phases

**Current phase: Phase 8 — Hardening & Operations** (Phases 1–7: COMPLETE)

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

## Phase 5 — Adaptive Document-Aware Chunking — COMPLETE

**Goal:** One adaptive chunking strategy that handles any kind of document, reached by adding readers for more formats and better prose splitting, and by measuring real retrieval quality instead of adding strategy after strategy.

**Design principle (held).** The chunker (`document_aware`) works on an abstract element model. What differs between a PDF, Markdown, HTML, a spreadsheet, or an email is the reader that produces those elements, not the chunking algorithm. New formats are new readers emitting the same elements; there is no strategy per format. `fixed_size` stays for plain text. Strategy choice stays manual and per document in `chunking.documents`.

**What was built**

1. **Measurement.** `scripts/ingest.py --dry-run` prints a chunk QA report per document and for the corpus — size distribution, oversized, empty, undersized, exact duplicates, chunks that start mid-sentence or end mid-word, chunks that are mostly a URL — and writes it as JSON with `--qa-json`. It needs no database and no API keys. `scripts/evaluate.py` measures retrieval itself (recall@k and MRR through the real `Retriever`, both searches included), records a baseline, and fails on a regression against it; `--check-dataset` separates a typo in the evaluation set from a retrieval failure. `evaluation/questions.yaml` holds 25 cases against the sample corpus. See `chunking-guide.md`.

2. **Readers, one element model.** `extraction/readers/`: PDF, Markdown, HTML (boilerplate removed, main content chosen by prose density), DOCX, CSV/TSV, Excel, JSON/JSONL/YAML, email (eml/mbox), source code (~40 languages, one element per top-level definition), and plain text with light structure detection as the fallback for any other extension. The element model gained `LIST_ITEM`, `CODE`, `QUESTION`, `ANSWER`, `SPEAKER_TURN` and `MESSAGE`, plus the context each needs (`lead_in`, `speaker`, `language`, `time_seconds`, line range; `page` doubles as sheet or slide number). Chunk rules per kind are in `chunking/document_aware.py`. PDF gaps closed: borderless tables (`extraction/pdf/borderless.py`, deliberately conservative) and an opt-in OCR fallback for scanned pages (`extraction/pdf/ocr.py`). See `adding-a-reader.md`.

3. **Prose splitting.** `prose_splitting: paragraph | sentence`, global or per document. Sentence segmentation is script-agnostic (`chunking/sentences.py`) and overlap is taken in whole sentences.

4. **Sizing.** Sizes are in tokens of the configured embedding model (`chunking/tokenizer.py`, tiktoken). `max_chunk_tokens` is a hard cap, derived from that model's input limit when unset and proven on every chunk by `chunking/assembly.py`, so nothing is silently truncated. Per-document `chunk_size`, `chunk_overlap` and `prose_splitting` overrides, all validated at load time.

5. **Optional extras: not adopted.** `semantic` prose splitting, `parent_child` and `contextual` are gated on the harness showing a gain, and none has been measured, so none is implemented. `chunking/prose.py` defines a `ProseSplitter` protocol so a semantic splitter drops in when there is a measurement to justify its cost. `propositions` remains not planned. The reasoning is recorded in `chunking-guide.md`.

6. **Groundwork for Phase 6.** `sources.chunking` (migration 0003) records the plan each source was ingested with: strategy, sizes, splitting mode, tokenizer. Phase 5 only records it; acting on a change is Phase 6's job.

**Measured result on the sample corpus** (9 documents, 5,767 chunks): zero defects — no chunk over its configured size, none over the hard cap, none empty. The report also surfaced two real bugs during development, both fixed: a token-budget estimate that let one chunk exceed `chunk_size`, and borderless-table detection misreading figure-caption text as table rows. The remaining flags (13 mid-sentence starts, 5 mostly-URL, 53 undersized, 1 duplicate) are properties of the source PDFs rather than chunking defects; `chunking-guide.md` explains how to read them.

**Retrieval baseline recorded** (`evaluation/baselines/retrieval.json`, 25 cases over the sample corpus, `text-embedding-3-small`, `top_k: 5`):

| metric | value |
|---|---|
| recall@1 | 0.680 |
| recall@3 | 0.960 |
| recall@5 | 1.000 |
| MRR | 0.828 |

Every case retrieved its expected passage; 17 of 25 at rank 1, 7 at rank 2, 1 at rank 5. The deliberately misspelled case ("how big is VY Canis Majorus") came back at rank 2, which is the typo-tolerant keyword search doing its job — vector search alone does not match a misspelled star name. Re-running against the baseline reports "unchanged" and exits 0, so the regression gate is live rather than nominal: from here, a change to chunking or retrieval that lowers a metric fails the check.

**Testing.** 336 tests pass, including the repository, vector-store and migration tests against a real Postgres (275 of them need no database). Element-level tests for every chunking rule, real small files per reader, and the edge cases that break real corpora: empty input, one huge paragraph, Unicode and CJK, non-UTF-8 encodings, CRLF line endings, a file that is not the format its extension claims.

**Done when (met):** the evaluation harness and QA report exist and both baselines are recorded (`evaluation/baselines/chunk-qa.json`, `evaluation/baselines/retrieval.json`); the readers listed above are implemented and tested; sentence-aware splitting works and is selectable per document; sizing is token-accurate with a hard cap; each source records the strategy and parameters it was chunked with; and no optional extra was adopted, so none needs a measured improvement.

## Phase 6 — Change Detection & Re-sync — COMPLETE

**Goal:** Keep the index in step with changing sources, and do it without re-reading work that has
not changed.

### What was built

**The extractor became lazy, and the skip moved in front of extraction.** `Extractor.extract()`
yielded fully-parsed documents, so there was nothing cheaper to ask it for. It is now
`Extractor.candidates()` (plus a `source_type`), yielding `SourceCandidate`s that carry a
`source_hash` of the raw bytes, the reader that would handle the file and that reader's version,
and parse only when `read()` is called. One method rather than two that can drift, laziness
visible in the type, and it streams — which also fixed the latent problem in Phase 5's pipeline,
where every document *and* every chunk list was held in memory before anything was stored.
`extraction.base.read_all()` is the adapter for the tools that want all the text and no change
detection (`ingest.py --dry-run`, `evaluate.py --check-dataset`).

**Two hashes, because they answer different questions.** `sources.source_hash` (migration 0004) is
of the raw bytes and gates "must I open this file at all"; `sources.content_hash` is of the
extracted text and gates "did opening it produce something different, so it needs re-embedding".
mtime and size were rejected as the cheap signal: hashing the corpus already costs 31ms, so a
`stat()` fast path saves nothing worth having, and mtime lies — cloud sync, `cp -p` and rsync
without checksums all preserve it while content changes.

**The whole decision is made before any file is opened** (`ingestion/changes.py`): a source is
opened only when it is new, previously marked `deleted`, changed in its bytes, or recorded against
a different chunking plan or reader version. The stored state for the source type is loaded in one
query, not one round trip per document, and the run logs the reason each document was opened.

**The recorded plan now names the reader.** `sources.chunking` gained `reader` and
`reader_version` alongside the chunking settings. A reader *improvement* changes the extracted text
without changing the file's bytes, so without this a better reader would never reach documents
already ingested; bumping the version re-extracts, re-chunks and re-embeds everything that reader
handles. Reader *configuration* (`extraction.pdf.skip_sections` and friends) is deliberately not
part of the record — bumping `PdfReader.version` is the manual lever, which `adding-a-reader.md`
documents.

**Deletion came for free from the candidate list**, which is exactly the set of sources that still
exist. Anything stored, not already deleted, and missing from it is marked `deleted` and loses its
chunks and vectors in one transaction. A source that comes back is ingested again, because
`deleted` is itself a reason to open.

**Reporting.** `scripts/ingest.py` prints added / updated / skipped / removed / failed, and the
line `opened N of M sources; K never had to be opened`. A source that cannot be read is now a
reported `failed` rather than a silent omission, and it is never mistaken for a deleted one.

### Measured result

On the sample corpus (9 files, 40.2MB), enumerating and hashing every source takes **0.031s**,
where parsing them takes **69.3s** — about a two-thousandfold saving on a run with nothing to do,
against the 49s/0.05s the phase was planned on. Chunk output is unchanged: `--dry-run` still
reports 5,767 chunks with zero defects, matching `evaluation/baselines/chunk-qa.json`.

One-time cost on upgrade: existing rows have no raw hash and their recorded plan does not name a
reader, so the first run opens every source. Sources whose extracted text is unchanged are not
re-embedded, so that pass costs parsing time and no embedding bill.

**Testing.** 352 tests pass (was 336), including against a real Postgres. New coverage: a raw hash
is of the bytes and not the text; candidates parse nothing until `read()`; an unchanged source is
skipped with the extractor never opened; only the changed document is opened; a file whose bytes
changed but whose text did not records the new hash without re-embedding; changed chunking settings
re-chunk an unchanged document; a bumped reader version re-extracts; a deleted source is marked
`deleted` and loses its chunks and vectors with no orphans; a source already deleted is not removed
twice; a deleted source that returns is re-ingested; an unreadable source fails without being
treated as deleted; a document with no configured strategy fails without being opened.

**Done when (met):** editing one source document and re-running ingestion opens and reprocesses
only that document — the others are skipped on their raw hash without being parsed, and the run
time shows it plainly — with no orphaned chunks or vectors left behind; changing a document's
chunking settings in config re-chunks it on the next run; deleting a source file marks it
`deleted` and removes its chunks and vectors; and bumping a reader's recorded version re-extracts
the documents that reader handles.

## Phase 7 — API & Authentication — COMPLETE

**Goal:** The public integration surface, testable with curl.

### What was built

**`POST /api/query`** (`api/query.py`), with the request and response models from
`architecture-context.md` §5. `question` is trimmed and must not be blank, `filters.source_type`
narrows retrieval, and an unknown field is rejected rather than ignored — a typo in a payload is an
error, not a filter that silently did nothing. The handler stays thin: the wiring moved to
`services/factory.build_query_service`, which the API and `scripts/ask.py` now share, so the two
cannot come to answer the same question differently.

**API-key authentication** (`services/api_keys.py`, `api/dependencies.py`). A key is 32 random
bytes behind a `rag_` prefix; only its SHA-256 is stored, the lookup is by hash, and the final
comparison is `hmac.compare_digest`. A key is high-entropy, so a fast hash is the right one: there
is no low-entropy secret to brute-force, and a slow KDF would be paid on every request instead of
once. An unknown key and a revoked one get the identical 401, so a caller cannot use the response
to learn that a key once existed. Authentication is a dependency, so it resolves before the body is
validated: a request with no key learns nothing about the payload shape and never reaches retrieval.

**Key management** (`scripts/create_api_key.py`): `--label` issues a key and prints it exactly once,
`--list` shows every key with its state, `--revoke <id>` deactivates one after confirmation
(`--yes` skips the prompt, and an unreadable answer counts as "no"). Revocation is immediate — the
next request with that key is a 401.

**One error shape** (`api/errors.py`): every failure is `{"error", "code"}`. FastAPI's own 422 is
mapped to 400 `invalid_request`; a missing or blank key is 401 `missing_api_key` and an unknown or
revoked one 401 `invalid_api_key`; an `EmbeddingError`, `LLMError` or `SQLAlchemyError` is 503
`upstream_unavailable`, with the provider's own message logged rather than returned to the caller.
404 and 405 come back in the same shape. An empty retrieval is not an error: the LLM is still
called with an empty context and the answer is a normal 200 with `sources: []`.

**Input limits**: `api.max_question_chars` (default 2000) is configuration rather than a constant in
the route, and is enforced before the question reaches the embedding or LLM call.

**Start-up and shutdown**: a lifespan opens the database engine and the provider clients once and
disposes of them on shutdown, instead of rebuilding them per request. Everything else a route needs
arrives through `Depends`, which is what lets the tests replace the database and both providers.

**Accurate OpenAPI**: the configured API-key header is declared as a security scheme, so `/docs`
has a working authorise button; each error status names `ErrorResponse`; and the automatic 422 is
removed from the document, because a status code the API never returns is a branch a generated
client can never run.

### Verified end to end

Against the ingested sample corpus (11 sources, 5,928 chunks), with a real key issued by the CLI:
the `get-started.md` curl example returns 200; "What is the largest known star?" comes back grounded
in `List_of_largest_stars.pdf` with its citations; a missing key, an unknown key, a payload typo, a
blank question and a 2,001-character question return 401 / 401 / 400 / 400 / 400 in the standard
shape; and the same key returns 401 immediately after `--revoke`.

**Testing.** 392 tests pass (was 352). The 40 new ones need no database and call no provider: the
grounded answer with its sources, question trimming, filters reaching the retriever, the
empty-retrieval 200, missing / blank / unknown / revoked keys, authentication before body
validation, nine malformed payloads, the configured length cap, each upstream failure as a 503 that
leaks no provider detail, a database failure during authentication as a 503 rather than a 401, 404
and 405 in the same shape, and the OpenAPI document's responses, security scheme and absent 422.

**Done when (met):** the curl example in `get-started.md` works end to end, and tests cover a
missing key, a revoked key, a malformed request and the empty-retrieval path.

**Left to Phase 8 on purpose:** per-key rate limiting, `query_logs` and `last_used_at` updates
(`ApiKeyRepository.touch_last_used` stays unused until then, so `--list` shows every key as never
used), the internal/admin paths, and timeouts and bounded retries beyond those the provider clients
already apply.

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
