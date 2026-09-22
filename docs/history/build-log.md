# Build Log — how this template was built

**This is history, not instruction.** It records the nine phases in which the template itself was
built, in dependency order, with what each one delivered and why. It is kept because the reasoning
is often the useful part — why the skip moved in front of extraction, why the rate-limit count is
in Postgres, why an optional chunking strategy was measured and then not adopted.

**It is not a guide to your project, and nothing here is a task list for you.** Every phase below
is already done in the code you have. The figures are measurements of the template's own sample
corpus at the time of writing — test counts, chunk counts, timings, retrieval scores — and they
describe a corpus you have deleted. Do not treat them as targets, and do not update this file as
your project progresses; keep your own notes instead.

**The corpus these figures were measured on.** Phases 5 to 9 were measured against nine Wikipedia
PDFs (about 40MB) in `data/sample`, which is where the chunk counts, the 69.3s parse time and the
retrieval scores below come from. They were removed when this repository became a template, since
a template should not ship somebody else's 40MB corpus — but they are still in git history, so
`git log --diff-filter=D -- data/sample` finds the commit that removed them and any figure below
can be reproduced by checking out an earlier commit. The sample corpus now is two short documents
about RAG itself, kept only so a clean clone can run `--dry-run` immediately.

For the decisions that still bind new work, see `context/design-decisions.md`. For what the system
is and how it is put together, see `context/architecture-context.md`.

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

## Phase 8 — Hardening & Operations — COMPLETE

**Goal:** Safe and reliable to run in a real environment.

### What was built

**Per-key rate limiting, counted in Postgres** (`services/rate_limit.py`, migration 0005). A fixed
window per key: `api_key_rate_limits` holds one row per (key, window) and the count is a single
atomic upsert that returns the new value. The counting is in Postgres and not in the process
because production runs several Uvicorn workers (Phase 9) — an in-memory counter would give each
worker the whole allowance, so the effective limit would be the configured one times the worker
count, which is a limit that is not the limit. Read-then-write was rejected for the same reason at
a smaller scale: two workers would both read 59 and both allow the 60th.

The window is fixed rather than sliding. The cost is a boundary burst — a caller can spend its
allowance at the end of one window and again at the start of the next — and the alternative costs
a stored row per request. For protecting an LLM bill from a runaway consumer, the burst is
affordable and the row per request is not. Over-limit requests are refused with 429
`rate_limit_exceeded` plus `Retry-After`, and the allowance is reported on successful responses too
(`X-RateLimit-Limit`, `X-RateLimit-Remaining`) so a consumer can pace itself rather than wait to be
refused. A refused request still counts: otherwise hammering a closed door would be free.
`api.rate_limit.requests: 0` turns limiting off and spends no round trip doing so. Stale windows
are purged when a key opens a new one — one delete per key per window, so the table stays at about
a row per active key rather than growing for the life of the deployment.

**Usage recording** (`services/usage.py`). `api_keys.last_used_at` is always written, because it is
what makes a key accountable — it is how an operator finds the key nobody uses and revokes it.
`query_logs` is written only when `api.log_queries` is on, because questions and answers are user
data and a deployment holding sensitive questions should be able to keep the key accounting without
the content. The two have deliberately different failure rules: `last_used_at` and the rate-limit
count share one commit and run *before* the question is answered, so their failure is the
request's failure; the query log runs *after*, when the LLM call has already been paid for, and a
failure there is logged and swallowed — losing a generated answer to a bookkeeping fault would
turn a minor error into a failed request.

**Internal admin paths** (`api/admin.py`, `services/admin.py`, `services/jobs.py`, migration 0006).
`POST /internal/ingest` (which is re-sync too: a run opens only what changed),
`GET /internal/ingest[/{id}]`, `DELETE /internal/ingest/{id}`, and `GET|POST /internal/api-keys`,
`DELETE /internal/api-keys/{id}`.

Three things keep it out of reach. It is **not mounted at all** unless `ADMIN_API_TOKEN` is set — an
operations surface nobody configured should not be reachable, and the tests assert 404 rather than
401 in that case. It needs its own token in its own header, compared with `hmac.compare_digest`, so
a consumer key issued to a frontend can never issue itself another one or start a run. And every
route is `include_in_schema=False`, so a consumer reading `/docs` never learns the surface exists.

Ingestion is *started* there, not performed there: a run takes minutes and a request handler is the
wrong place to spend them, so the route claims a job and answers 202 with an id to poll.
`ingestion_jobs` holds that state because the run outlives the request and the poll may reach a
different worker, and a partial unique index (`state = 'running'`) is what makes "one run at a
time" true across workers rather than within one. A run whose worker is killed leaves its row
`running`, which is visible and fixable — `DELETE /internal/ingest/{id}` releases it — rather than
silently lost, or silently restarted on top of a run that may still be alive.
`services/ingestion.build_ingestion_pipeline` is now the single wiring of the ingestion path,
shared with `scripts/ingest.py`, so the CLI and the route cannot come to ingest differently.

**Timeouts and bounded retries, from config rather than constants.** `models.llm_timeout_seconds`,
`llm_max_retries`, `embedding_timeout_seconds` and `embedding_max_retries` replace the module
constants in the provider modules: what a caller will wait for is a deployment decision, and one
codebase serves clients whose corpora and latency budgets differ. A failure that outlives the SDK's
bounded retries — a timeout included — arrives as `LLMError` or `EmbeddingError` and is answered
503, never as a silently empty answer.

The vector DB and every repository read are Postgres, so one `database.statement_timeout_seconds`
covers all of them. It is a *server* setting, so Postgres cancels the query rather than the client
walking away from one that keeps running, and a query that would never return costs a 503 instead
of a worker held open indefinitely. Statements are deliberately **not** retried inside a request: a
blind retry of something that may have been a write is worse than the failure it hides, and
`pool_pre_ping` already covers the one case worth retrying transparently — a connection the server
has already closed. The bound is the API's alone; the CLI scripts pass none, because cancelling an
ingest transaction half way only wastes the embedding bill already paid.

**Prompt-injection safeguards** were already in place from Phase 4 and are now tested as such:
retrieved text and its `source_uri` are escaped and delimited (`generation/prompt.py`), so a
passage cannot close `<context>` or open a `<question>` and be promoted from data to instruction,
and the system prompt names the context as untrusted. Tested against five real injection shapes,
both at the prompt level and through the whole query path.

**Structured logging that cannot leak a secret** (`observability.py`). `logging.format: text |
json`; `json` is one object per line, with anything passed as `extra` becoming an indexed field.
Redaction happens **in the formatter**, not at the call site, and that is the point: a filter or a
careful caller can be forgotten, but every record — including ones from libraries this project does
not own, and including exception tracebacks — passes through a formatter. It covers the shapes a
secret takes here (an issued `rag_` key, a provider key, a password inside a database URL, a bearer
token) and, once settings are loaded, this deployment's exact secret values, which catches a
password that happens to look like an ordinary word. `install_handler` replaces only the handler it
installed, so it never silently switches off one that something else attached.

`api/middleware.py` logs one line per request — method, path, status, duration, and the *id* of the
key, never its value — and returns an `X-Request-ID` an operator can search for. The question is
not in that line: it is user data, and it belongs in `query_logs`, where a deployment can turn it
off.

### Testing

**542 tests pass (was 392).** The 150 new ones cover: the whole rate-limit decision over a clock a
test moves by hand (window alignment, refusal, a refused request still counting, separate
allowances per key, the allowance returning with the next window, purging once per window rather
than once per request, `Retry-After` never zero); the atomic upsert, the partial unique index and
the state constraint against a real Postgres; 429 through the API with its headers, and that an
over-limit caller is refused before the body is parsed and never reaches retrieval; `last_used_at`;
query logging on, off, with an empty retrieval, and with a write that fails without costing the
caller their answer; that 401, 400 and 429 are never query-logged; redaction in both formatters, of
a log argument and of an exception traceback; that the request line holds neither the key nor the
question; the admin surface absent without a token, 401 with the wrong one, unreachable with a
consumer key, absent from OpenAPI, and authorising before body validation; the job lifecycle
including a second run refused, a dead run released, a finished run not releasable, and a run that
raises recorded as failed rather than left `running`; the configured timeouts and retries reaching
both SDK clients, and a timeout surfacing as a 503; the statement timeout reaching the driver in
milliseconds; and every new config value defaulting, validating, and rejecting a misspelt key
rather than ignoring it.

**Done when (met):** rate limits and upstream failure behaviour are verified by tests, and no
secrets or plaintext keys appear in logs.

**Left to Phase 9 on purpose:** the production `Dockerfile` and Gunicorn, a second provider behind
each interface, the CORS/origin allowlist decision, and the per-client deployment template and
onboarding runbook.

## Phase 9 — Extensibility & Deployment — COMPLETE

**Goal:** Ready to deploy, and ready to stand a new client up without forking the codebase.

### What was built

**A production image** (`Dockerfile`, `docker/gunicorn.conf.py`). Two stages, so the compiler that
builds wheels does not ship: the runtime stage copies the finished virtualenv and installs nothing
from the network. It runs as UID 10001, owns none of the application files and writes nothing —
`chunked/` is a `--dry-run` artefact and is not produced in production. `/health` needs no key, so
the healthcheck needs no credential in the image.

Gunicorn supervises Uvicorn workers, and it is there for supervision rather than concurrency: the
workers are async, so concurrency comes from the event loop, and Gunicorn is what restarts a worker
that dies and reaps one that hangs. Hence one worker per core rather than the usual `2 × cores + 1`
for sync workers, and a default of two — each worker holds its own database pool and its own
provider clients, so more is not free. `preload_app` is off: the lifespan opens those pools per
worker, and preloading would fork them into every child as shared file descriptors. The Gunicorn
timeout (180s) is deliberately longer than the longest upstream bound plus retries, so it kills a
worker that has genuinely hung rather than one waiting on a slow but healthy provider.

**Migrations are their own step**, not the image's `CMD` and not worker start-up: every worker
would race the others, and with more than one replica the schema would be applied by whichever
container happened to boot first. The Compose template runs a `migrate` service to completion and
the API waits on `service_completed_successfully`.

**Per-client deployment from one template** (`deploy/`). `deploy/docker-compose.client.yml`
parameterised by `CLIENT`, plus a directory per client holding the three things that differ: its
`config.yaml`, its `.env`, and its `documents/`. The Compose project name is prefixed by the client
name, so two clients on one host get separate volumes and containers and no way to reach each
other's data. The database publishes no port. Ingestion is a `profiles`-gated job rather than a
service, so it runs on demand and not at start-up. `deploy/clients/example/` is the copy-me
template, and a test loads its `config.yaml` so it cannot drift from the schema.

**A customisation boundary** (`customisations/`, `customisations/README.md`). One config value —
`customisation: acme` — loads `customisations/acme`, which exposes a single
`customisation = Customisation(...)` object saying what it provides. Two hooks, each an existing
Protocol: extra `Reader`s (registered *ahead of* the built-ins, so a client whose `.md` files are
not Markdown can say so) and extra `Extractor`s keyed by `extraction.provider` name.

The load fails loudly — a missing module, a missing `customisation`, one of the wrong type — rather
than falling back to none, because a client's reader silently not loading means their documents are
ingested by the plain-text fallback, which looks like working software and produces wrong answers.
It resolves at start-up, in `create_app` and when the pipeline is built, and the deployment logs
what it loaded.

`extraction.provider` therefore cannot be a closed `Literal` any more: which providers exist
depends on a module `settings.py` deliberately does not import. An unknown name is rejected in
`extraction/factory.build_extractor` instead, naming the ones available — at the start of a run
rather than part way through one.

What the boundary refuses is the point: not a fork of a pipeline stage (a client-specific chunking
rule is a missing field on the element model, which belongs in the shared codebase), not a second
source of configuration, not a place for secrets.

**A second LLM provider** (`generation/openai_provider.py`). `models.llm_provider: openai` now
works, which is what makes `LLMProvider` an interface rather than the shape one class happens to
have — and it answers a real onboarding question, since a client with only an OpenAI account is now
a config change rather than a project. The contract's guarantees are preserved in the new
implementation, not inherited from an SDK: bounds from config, and every failure mode (a transport
error, a timeout past the retries, no choices, empty text) raised as `LLMError` and answered 503
rather than returned as an empty answer. `build_llm_provider` checks each provider's secret at
build time and names the variable to set.

**The provider lists now say only what exists.** `embedding_provider` was `openai |
sentence_transformers` and `vector_db.provider` was `pgvector | pinecone | qdrant | weaviate`,
but three of those five raised `RuntimeError` when selected — a setting that could not take effect,
which is exactly what code-standards.md forbids. Both are narrowed to what is implemented, so
choosing one that is not is a load-time error naming the alternatives. Adding one is a new
implementation behind the interface that already exists plus a name in that list; see *Considered
and not adopted* for why neither was built now.

**CORS, decided rather than configured by default** (`main.install_cors`, `api.cors`). Empty by
default, and the middleware is not installed at all when it is empty, because a consumer frontend
calls this API from its own server where CORS does not apply and where its key can be held safely.
A browser calling here directly would have to hold that key itself, which it must never do — so an
entry is a deliberate exception. `*` is refused outright, and an origin without a scheme is
refused; allowed methods and headers are narrowed to what this API actually has.

**Documentation for standing a client up.** `context/client-onboarding.md` is the runbook —
provision, collect the real corpus, configure, check the chunks before storing anything, ingest,
write *their* evaluation set and record both baselines, issue the key, verify five behaviours, hand
over the URL and key and nothing else. `context/adding-an-extractor.md` covers a new data source,
distinguishing it from a new format: the two rules that matter are that `candidates()` parses
nothing (the 0.031s-versus-69s decision Phase 6 exists for) and that partial enumeration must raise
rather than return, because the candidate list is also the set of sources that still exist and a
half-enumeration would delete the rest of a client's corpus.

### Verified end to end

The image was built and the per-client stack run for real (`CLIENT=example`, two Gunicorn
workers, a live Postgres), not just unit-tested:

* The image builds in two stages and runs as `uid=10001(rag)`. Started without `DATABASE_URL` it
  refuses to boot, naming the variable, rather than serving requests that cannot work.
* The `migrate` service applied 0001–0006 and exited; the API then started behind it.
* `/health` 200 with an `X-Request-ID`; `/api/query` with no key 401 `missing_api_key`;
  `/internal/api-keys` 401 `missing_admin_token` with no token, 401 `invalid_admin_token` with a
  wrong one, 200 with the right one.
* A key issued over `POST /internal/api-keys` authenticated on `/api/query`, and the key's
  `last_used_at` was recorded.
* **Rate limiting across workers:** 63 requests against a limit of 60 gave exactly 60 through and
  3 × 429 with `Retry-After` and the allowance headers. Two worker processes sharing one
  allowance is the whole reason the count is in Postgres, and this is the test that would have
  failed had it been in memory.
* `POST /internal/ingest` returned 202, the job was polled to `succeeded`, and the report and
  recent-runs listing came back as expected.
* **No secrets in the logs:** across 2,646 lines, zero occurrences of the provider keys, the
  admin token, the database password or any `rag_` key, with 186 values redacted — including
  inside the OpenAI SDK's own 401 message and its traceback, which is what redacting at the
  formatter rather than the call site buys. No question text appeared either.

One gap that only a live run would have shown: the allowance headers were set by the route, so a
response built by an exception handler (a 503, a 400) lost them. Applying them in the middleware
instead fixes it — being told to slow down should not depend on the request having succeeded.

### Testing

**589 tests pass (was 542).** The 47 new ones cover: the customisation boundary end to end — a
client reader loaded, described, winning a contested extension, reaching the extractor and chunking
through the same rules as any other format; a client extractor selected by provider name and
honouring `ExtractionError`; and every way of getting the name wrong stopping start-up, including
an error inside the client module not being swallowed. Written into a temporary package, so the
test file doubles as the worked example. Then: the OpenAI provider's message shape, its five
failure modes as `LLMError`, and one config value choosing between the two providers with each
one's secret checked by name; CORS absent by default, present for a named origin, refusing an
unnamed one, the preflight allowing the key header and only GET/POST, and credentials off unless
asked for; the app refusing to start on an unresolvable customisation and reporting a loaded one;
the unimplemented providers rejected at load time while an unknown *extraction* provider is not
(it is the factory's job, and it names what exists); and the example client's `config.yaml`
loading, so the deployment template cannot drift; and the allowance headers surviving a 503,
absent on a request that never authenticated.

**Done when (met):** the image builds and runs against a production-style config; standing up a
new client is the runbook plus a config file rather than a code change; and a fix made for one
client can be applied to the others without a manual merge, because there is no fork to merge
into — client code lives behind the Protocols in a module the shared codebase does not own.

---