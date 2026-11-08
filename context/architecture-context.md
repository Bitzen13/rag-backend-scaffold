# Technical Architecture Document — RAG Backend

## 1. Tech Stack

- **Language/Framework:** Python, FastAPI (async, OpenAPI docs generated automatically)
- **ASGI server:** Uvicorn; in production Gunicorn supervises Uvicorn workers (`docker/gunicorn.conf.py`) — for supervision, not concurrency, since the workers are async
- **LLM provider:** Anthropic or OpenAI, both implemented behind one provider interface and selected by `models.llm_provider`
- **Embedding model:** OpenAI (`text-embedding-3-small` by default). `embedding/base.py` is the interface; a local model is *not* implemented, on cost — see *Considered and not adopted* in `design-decisions.md`
- **Vector database:** pgvector, which avoids a separate service. `vector_store/base.py` is the interface; the hosted alternatives are *not* implemented, for the same reason. Each config choice lists only what exists, so a provider that cannot take effect is a load-time error
- **Containers:** one production image for every client (`Dockerfile`, non-root, multi-stage); per-client deployment from one Compose template (`deploy/`)
- **Relational database:** PostgreSQL — ingestion metadata, chunks, API keys, query logs; the `pg_trgm` extension powers typo-tolerant keyword retrieval (GIN trigram index on `chunks.text`, migration 0002)
- **Containerization:** Docker + docker-compose for local dev
- **Secrets:** Environment variables only
- **Document parsing libraries:** one per format, isolated behind a reader in `extraction/readers/`: pdfplumber (PDF), beautifulsoup4 (HTML), python-docx (DOCX), openpyxl (Excel), plus the standard library for CSV, JSON, YAML, email and source code. Optional: pytesseract, for OCR on scanned PDF pages
- **Tokenizer:** tiktoken, so chunk sizes are measured in the embedding model's own tokens (falls back to word counts with a warning if unavailable)
- **Chunking libraries:** none. The splitters are in `chunking/` (`sentences.py`, `prose.py`) because what is needed is token-budgeted, script-agnostic splitting over the element model

## 2. System Boundaries

**In scope**
- Full ingestion pipeline (extraction → chunking → embedding → vector storage) with metadata tracking and change detection
- Retrieval and generation logic
- POST /api/query as the sole public integration surface
- API key issuance/revocation and per-key rate limiting
- Configuration management

**Out of scope**
- Any UI — this repo produces no rendered interface, only an API
- Serving more than one client company from a single deployment; one deployment per client is the chosen model
- End-user identity/auth (only the calling application is authenticated, via API key)
- Long-term conversational memory across requests (each call is stateless unless the caller passes history in the payload)
- Canonical storage of original source documents (only derived/extracted data is stored)
- Model fine-tuning/training

**External boundaries**
- Inbound: HTTP requests to /api/query from any authenticated consumer (frontend, Slack bot, mobile app)
- Inbound (ops-only): ingestion triggered via CLI/internal admin endpoint, not publicly exposed
- Outbound: LLM provider API, embedding provider API, vector database, Postgres

## 3. Database Structures

**Single-tenant by design.** Every table below holds one client's corpus, and an API key
identifies a calling application within that one deployment. Serving several client companies
means running this stack once per client, each with its own database and its own documents —
the reasoning, and the findings that would matter if that is ever revisited, are recorded in
`design-decisions.md`.

### 3.1 Postgres schema

**sources**
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| source_type | text | e.g. pdf, web, notion, csv |
| source_uri | text | path/URL/identifier |
| source_hash | text | SHA-256 of the source's raw bytes; gates whether the file is opened at all (migration 0004) |
| content_hash | text | SHA-256 of the extracted text; gates whether it is re-chunked and re-embedded |
| last_ingested_at | timestamptz | |
| status | text | active / stale / deleted (enforced by a CHECK constraint) |
| metadata | jsonb | source-specific metadata, including which reader produced it |
| chunking | jsonb | the chunking plan this source was last ingested with (migration 0003) |

Constraint: `UNIQUE (source_type, source_uri)` — a source is identified by its type and URI, which is what change detection looks up.

`chunking` holds `{strategy, chunk_size, chunk_overlap, prose_splitting, max_chunk_tokens, tokenizer, reader, reader_version}`. Every run compares it against what `config.yaml` and the registry would produce now, so changing a document's chunking settings — or bumping a reader's version — re-chunks it, which no hash of the file can detect. The comparison needs no extraction: it is config against a stored value.

**Two hashes, because they answer different questions.** `source_hash` is of the file's raw bytes and answers "must I open this at all"; `content_hash` is of the extracted text and answers "did opening it produce something different, so it needs re-embedding". Neither replaces the other. mtime and size are deliberately not used as the cheap signal: hashing the whole corpus costs 31ms, so a `stat()` fast path saves nothing worth having, and mtime lies — cloud sync, `cp -p` and rsync without checksums all preserve it while content changes, and a silently stale chunk is a wrong answer.

**chunks**
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | matches vector DB chunk_id |
| source_id | UUID (FK) | |
| chunk_index | int | |
| text | text | retained for citation and re-embedding |
| token_count | int | exact count from the tokenizer recorded in `sources.chunking` |
| embedding_model | text | for auditability |
| created_at | timestamptz | |

Constraint: `UNIQUE (source_id, chunk_index)`; `source_id` cascades on delete.

**api_keys**
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| key_hash | text | never store plaintext; UNIQUE |
| label | text | e.g. "Next.js frontend — Client A", "Slack bot" |
| is_active | boolean | revocation flag |
| created_at | timestamptz | |
| last_used_at | timestamptz | |

**query_logs** — written per answered question when `api.log_queries` is on
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| api_key_id | UUID (FK) | |
| question | text | user data; this is what `api.log_queries: false` exists to not store |
| answer | text | |
| retrieved_chunk_ids | UUID[] | empty when nothing passed the similarity threshold |
| created_at | timestamptz | |

**api_key_rate_limits** (migration 0005) — one row per key per fixed window
| Column | Type | Notes |
|---|---|---|
| api_key_id | UUID (PK, FK) | cascades on delete, so revoking and deleting a key cleans up |
| window_start | timestamptz (PK) | aligned to the epoch, so every worker agrees which window |
| request_count | int | incremented and returned by one atomic upsert |

Counted in Postgres rather than in the process: production runs several Uvicorn workers, and an
in-memory counter would hand each of them the whole allowance, making the effective limit the
configured one times the worker count. One statement, not read-then-write, for the same reason a
size larger: two workers would otherwise both read 59 and both allow the 60th. A key's finished
windows are deleted when it opens a new one, so the table holds about a row per active key.

**ingestion_jobs** (migration 0006) — a run started through the internal admin path
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| state | text | running / succeeded / failed / abandoned (CHECK constraint) |
| started_at | timestamptz | |
| finished_at | timestamptz | null while running |
| detail | text | the counts, or the error that ended the run |
| report | jsonb | `{counts, opening, failures}` — what `scripts/ingest.py` would have printed |

Partial unique index on `state` where `state = 'running'`: that constraint, not a check in one
process, is what makes "one run at a time" true across workers. A row is needed at all because the
run outlives the request that started it and the poll asking how it went may reach a different
worker. A run whose worker is killed leaves its row `running` until an operator releases it
(`DELETE /internal/ingest/{id}`) — visible and fixable, rather than silently lost or silently
restarted on top of a run that may still be alive.

### 3.2 Vector database structure
Each record: chunk_id (join key to Postgres), embedding (float array), and a metadata payload (source_id, source_type, chunk_index, tags/date for filtering). Full chunk text lives in Postgres, not only in the vector DB payload, to avoid provider payload-size limits and simplify re-embedding.

**pgvector implementation (default):** table `chunk_vectors` — `chunk_id` (UUID PK, FK → `chunks.id` ON DELETE CASCADE), `embedding` (`vector(1536)`), `source_id` (FK → `sources.id` ON DELETE CASCADE), `source_type`, `chunk_index`, `metadata` (jsonb). Indexed with HNSW using cosine distance (`vector_cosine_ops`); search returns cosine similarity (`1 - distance`). The column width is fixed by the migration (`EMBEDDING_DIMENSIONS` in `db/models.py`, 1536 for `text-embedding-3-small`); switching to an embedding model with a different size requires a new migration that rebuilds `chunk_vectors`. The store rejects vectors of the wrong size.

**Data-access conventions:** repositories (`db/repositories/`) and `PgVectorStore` are bound to an `AsyncSession`, flush but never commit — the caller owns the transaction, so Postgres rows and vectors can be written or removed atomically. The `VectorStore` Protocol lives in `vector_store/base.py`; similarity thresholding is applied by the retrieval layer, not the store.

## 4. Configuration Structure

customisation: acme                     # optional; loads customisations/acme (see §8)

extraction:
  provider: filesystem                 # or a name a customisation registers
  path: data/sample
  extensions: [.md, .txt, .pdf]        # a reader is chosen per extension; unknown = plain text
  pdf:
    strip_patterns: [...]              # regular expressions; corpus cleanup, not structure
    truncate_patterns: [...]
    skip_sections: [References]
    borderless_tables: true            # also detect tables aligned with whitespace
    ocr:                               # read pages that are an image of text (needs Tesseract)
      enabled: false
      language: eng
      min_characters: 20
      resolution: 300
  limits:                              # a file over one is rejected by name; the run carries on
    max_file_bytes: 104857600
    max_archive_uncompressed_bytes: 524288000  # DOCX/XLSX, as their zip directory declares
    max_spreadsheet_cells: 2000000     # per sheet, from its declared dimensions
    max_extracted_chars: 20000000
    max_chunks_per_document: 20000
    max_metadata_bytes: 65536
    max_delete_fraction: 0.5           # a run deleting more stops first (--allow-mass-delete)

chunking:
  chunk_size: 500                      # sizes are in TOKENS of the embedding model below
  chunk_overlap: 50
  max_chunk_tokens:                    # hard cap; unset = derived from the model's input limit
  tokenizer: auto                      # auto (the embedding model's own) | words
  prose_splitting: sentence            # paragraph | sentence
  documents:            # strategy chosen manually for EACH document; unlisted = not ingested
    handbook.md: document_aware          # fixed_size | document_aware
    reference-tables.pdf: document_aware
    long-essay.pdf:                      # or a mapping, to override the settings above
      strategy: document_aware
      chunk_size: 800
      chunk_overlap: 60
      prose_splitting: sentence

retrieval:
  top_k: 5
  similarity_threshold: 0.4
  excerpt_max_chars: 300
  fuzzy_top_k: 5                       # typo-tolerant keyword hits per question (0 = off)
  fuzzy_min_similarity: 0.6            # pg_trgm word_similarity; 1.0 = word appears verbatim
  fuzzy_max_matches_per_phrase: 50     # a phrase matching more chunks than this is too common
  fuzzy_min_relative_evidence: 0.5     # drop fuzzy hits weaker than this fraction of the best

models:
  embedding_provider: openai
  embedding_model: text-embedding-3-small
  llm_provider: anthropic
  llm_model: claude-sonnet-4-6
  llm_max_tokens: 1024
  llm_timeout_seconds: 60              # bounds on every provider call; a failure that
  llm_max_retries: 3                   # outlives the retries is answered 503
  embedding_timeout_seconds: 30
  embedding_max_retries: 3

vector_db:
  provider: pgvector                   # the only one implemented; see design-decisions.md

auth:
  api_key_header: X-API-Key

api:
  max_question_chars: 2000             # rejected with 400 before any embedding or LLM call
  log_queries: true                    # store each answered question in query_logs
  rate_limit:
    requests: 60                       # per window, per key; 0 turns limiting off
    window_seconds: 60
  cors:
    allowed_origins: []                # empty = the middleware is not installed at all (§7)
    allow_credentials: false

database:
  statement_timeout_seconds: 30        # Postgres cancels a longer query; covers both searches
  connect_timeout_seconds: 10

logging:
  level: INFO
  format: text                         # text | json - json is one object per line, for a shipper

safety:
  output:
    allowed_link_hosts: []             # exact hosts whose links survive; images never do

Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY, DATABASE_URL, VECTOR_DB_URL, and the optional
ADMIN_API_TOKEN) come exclusively from environment variables.

Validation at load time, so a bad value fails loudly rather than producing bad chunks: every effective `chunk_overlap` is below its `chunk_size`, every `chunk_size` is within the hard cap, the hard cap is within the embedding model's input limit, `prose_splitting` is only set on `document_aware`, and an embedding model whose input limit is not known requires `max_chunk_tokens` to be set explicitly.

## 5. API Design

**POST /api/query** (`api/query.py`; dependencies in `api/dependencies.py`)
- **Auth:** the header named by `auth.api_key_header` (default `X-API-Key`), looked up by SHA-256 against `api_keys.key_hash` and rejected unless `is_active`. It is a FastAPI dependency, so it resolves *before* the body is validated: a request with no key never learns what the payload should look like, and never reaches retrieval.
- **Request:** { "question": "string", "filters": { "source_type": "optional" } } — `question` is trimmed, must not be blank, and must be at most `api.max_question_chars`; an unknown field is rejected rather than ignored.
- **Response:** { "answer": "string", "sources": [{ "chunk_id": "uuid", "source_uri": "string", "excerpt": "string" }] }
- **Untrusted fields:** `answer`, `excerpt` and `source_uri` are derived from the ingested documents, and a document can contain anything. Before they leave the service, `safety/output.py` removes every image and every link whose host is not in `safety.output.allowed_link_hosts` (Markdown inline and reference-style, raw HTML, autolinks, bare URLs; never a `javascript:` or `data:` URL). That stops an answer making a client fetch a URL. It does not make the text safe to render: **a client must escape all three fields** before showing them as HTML or Markdown. A `source_uri` that contained a link comes back without it, so it can differ from the stored name; `chunk_id` is the exact reference.
- **Errors:** every failure is `{ "error": string, "code": string }` (`api/errors.py`). `code` is the stable part; renaming one is a breaking change.

| status | code | when |
|---|---|---|
| 401 | `missing_api_key` | the API-key header is absent or blank |
| 401 | `invalid_api_key` | the key is unknown **or** revoked — one answer for both, so the caller cannot learn that a key once existed |
| 400 | `invalid_request` | malformed body, unknown field, blank question, or a question over `api.max_question_chars` (FastAPI's own 422 is mapped here) |
| 429 | `rate_limit_exceeded` | this key has spent its allowance for the current window; `Retry-After` gives the seconds until the next one |
| 503 | `upstream_unavailable` | the embedding provider, the LLM or the database failed; the provider's message is logged, never returned |

**Rate limiting** is a dependency, not middleware, so it resolves before the body is validated: an
over-limit caller is turned away before anything is parsed, embedded or sent to a provider, which
is the whole point of limiting. Every response — 200 and 429 alike — carries `X-RateLimit-Limit`
and `X-RateLimit-Remaining`, so a consumer can pace itself instead of waiting to be refused. A
refused request still counts against the window: otherwise hammering a closed door would be free.

**Per-request bookkeeping.** `api_keys.last_used_at` and the rate-limit count share one commit and
happen before the question is answered, so a request that later fails has still been accounted
for. The `query_logs` row is written after the answer exists and, when it fails, is logged and
swallowed — the LLM call has already been paid for, and losing its answer to a bookkeeping fault
would turn a minor error into a failed request.

An empty retrieval is **not** an error: the LLM is still called, with an empty context, and the response is a normal 200 with an empty `sources` list.

This request/response shape is the contract the frontend template (and any other consumer) is built against — changes to it must be versioned or made backward-compatible, since consumers are now maintained in separate repos.

**Key management:** `scripts/create_api_key.py --label ...` issues a key and prints it exactly once (only its hash is stored, so it cannot be recovered), `--list` shows every key and its state, and `--revoke <id>` sets `is_active` false, which takes effect on the next request.

**OpenAPI:** the configured API-key header is declared as a security scheme, so `/docs` can authorise; every error status names `ErrorResponse`; and FastAPI's automatic 422 is stripped from the document, since this API answers 400 instead.

**Start-up:** a lifespan opens the database engine and the provider clients once (`api.dependencies.build_resources`) and disposes of them on shutdown; `services/factory.build_query_service` is the single wiring of the query path, shared with `scripts/ask.py`.

**Internal/admin (not public)** — `api/admin.py`, mounted at `/internal`:

| route | does |
|---|---|
| `POST /internal/ingest` | start a run (re-sync is the same operation); 202 with a job id, 409 if one is already running |
| `GET /internal/ingest` | the recent runs |
| `GET /internal/ingest/{id}` | one run's state and report |
| `DELETE /internal/ingest/{id}` | release a run whose worker died, so the next can start |
| `GET /internal/api-keys` | every key and its state; never a hash or a plaintext |
| `POST /internal/api-keys` | issue a key; the response is the only place its plaintext exists |
| `DELETE /internal/api-keys/{id}` | revoke one; effective on the next request |

Three things keep this out of reach. It is **not mounted at all** unless `ADMIN_API_TOKEN` is set,
because an operations surface nobody configured should not be reachable. It needs that token in
`X-Admin-Token`, compared with `hmac.compare_digest` — the consumer keys in `api_keys` cannot
reach it, so a key issued to a frontend can never issue itself another one or start a run. And
every route is `include_in_schema=False`, so a consumer reading `/docs` never learns it exists.
The token is the second lock: a network boundary is still expected in front of it (§7).

Ingestion is started here, not performed here. A run takes minutes, and a request handler is the
wrong place to spend them, so the route claims a job row and answers 202 with an id to poll.
`services/ingestion.build_ingestion_pipeline` is the single wiring of that path, shared with
`scripts/ingest.py`, so the CLI and the route cannot come to ingest differently.

## 6. Data Flow Summary

**Ingestion:** Candidates (raw hashes, no parsing) → what changed? → Extractor → Chunker → Embedder → Vector DB (+ sources/chunks rows in Postgres, both hashes updated); sources that have gone are marked `deleted` and their chunks and vectors removed

Implemented in `ingestion/pipeline.py`, one transaction per document.

**Extraction is a reader per format, all producing one element model.** `extraction/base.py` defines the elements — heading, paragraph, list item, table row, code, question, answer, speaker turn, message — each carrying the context a chunk built from it will need (heading path, table caption, list lead-in, speaker, language, page/sheet/slide, line range). `extraction/readers/registry.py` maps an extension to its reader: PDF, Markdown, HTML, DOCX, CSV/TSV, Excel, JSON/JSONL/YAML, email (eml/mbox), source code, and plain text as the fallback for any other extension, so an unknown format is still ingested rather than skipped. A reader that cannot read a file logs it and the run continues. `extraction/elements.py` assembles every reader's output, joining element texts into the document text and giving each element its offsets in it. See `adding-a-reader.md`.

**Chunking is one algorithm over that element model**, so a new format never changes it. `document_aware` (`chunking/document_aware.py`) applies a rule per element kind: a table row is one chunk with its caption and column names; a code block stays whole; list items group under their lead-in; a question becomes the context its answers repeat; transcript turns group into a time window; a message is one chunk; paragraphs pack together under their heading path. A chunk's context line is counted against its token budget, not added on top of it, and a heading path too deep to fit is trimmed from the outside in. `fixed_size` remains for text with no structure worth preserving, and `document_aware` falls back to it for a document with no elements. Only one element can ever be oversized within a group, which is what keeps each split part's offsets exact. See `chunking-guide.md`.

**Sizes are in tokens of the embedding model** (`chunking/tokenizer.py`, tiktoken; word counts as a fallback with a warning). `chunking/sentences.py` finds sentence boundaries without assuming English — Latin, CJK, Devanagari, Arabic, Urdu and Ethiopic terminators, abbreviations and initials excluded, falling back to one span for text with no sentence punctuation. `chunking/prose.py` packs words or whole sentences into a token budget, overlapping by whole sentences in `sentence` mode and splitting by character as a last resort for text with no word breaks. Every chunk passes through `chunking/assembly.py`, which counts it exactly and enforces `max_chunk_tokens`, so no chunk is ever handed to the embedding model to be truncated.

**The PDF reader** (`extraction/pdf/`) works structure out per document from that document's own statistics, not fixed values: headings are lines larger than the document's body font (or short bold lines) with levels ranked by size; a table's first row is its header unless it is mostly numeric; a table starting a page right after one that reached the bottom continues it (repeated header rows are dropped); the short line above a table is its caption; running headers/footers are removed; text beside a figure is kept as separate paragraphs; multi-column pages are read column by column; paragraphs are joined across page breaks. Tables aligned with whitespace rather than ruling lines are found by `extraction/pdf/borderless.py`, which is deliberately hard to trigger — the rows must agree on column count and position, hold short values, contain no sentence running through a cell, and not read on into one another — because a false positive turns prose into nonsense rows. A page holding an image of text instead of text can be read with OCR (`extraction/pdf/ocr.py`, opt-in, needs Tesseract), which produces words of the same shape as any other page and so goes through the same layout analysis. Corpus-specific cleanup (citation markers, unrendered template text, sections to skip) is configured under `extraction.pdf` as regular expressions. Not supported: OCR of handwriting, `.doc`/`.xls`, slide decks.

**Change detection decides before extraction, not after it.** `Extractor.candidates()` enumerates the sources that exist without parsing any of them: each `SourceCandidate` carries its `source_hash`, the reader that would handle it and that reader's version, and parses only when `read()` is called. A run loads the stored state for its `source_type` in one query and opens a document only when it is new, previously marked deleted, changed in its bytes, or recorded against a different plan or reader version. On the sample corpus, enumerating and hashing all nine files (40MB) takes **0.031s** where parsing them takes **69s** — the whole performance case for this shape, and worth about two thousandfold.

Documents are then processed one at a time and streamed: nothing beyond a single document's chunks is ever held in memory. A changed source has its chunks and vectors replaced in the same transaction. A source that is opened but turns out to produce identical text has its new `source_hash` recorded and is not re-embedded.

**Deletion comes for free from the candidate list**, which is exactly the set of sources that still exist. Anything stored, not already deleted, and missing from it is marked `deleted` and loses its chunks and vectors in one transaction. Because an empty or wrongly mounted directory looks exactly like a corpus whose files were all deleted, a run that would delete more than `extraction.limits.max_delete_fraction` of the live sources raises `MassDeletionError` before it opens or deletes anything; `scripts/ingest.py --allow-mass-delete` lets one run through. The admin route has no override, by design: an operator who means it uses the CLI.

**What one file may cost is bounded** (`extraction.limits`, context/safety-plan.md §4). Enumeration refuses a symlink that resolves outside the extraction path and a file over `max_file_bytes` without reading either. Before a reader runs, a DOCX/XLSX whose zip directory declares more than `max_archive_uncompressed_bytes` is refused, and the Excel reader refuses a sheet whose declared dimensions exceed `max_spreadsheet_cells` before iterating it. YAML, both `.yaml` files and Markdown front matter, is loaded without expanding aliases (`extraction/yaml_safe.py`). After extraction, the pipeline refuses a document over `max_extracted_chars` or `max_metadata_bytes`, and one that chunks into more than `max_chunks_per_document`, before anything is embedded. Each refusal is a `LimitExceededError` whose message starts `rejected (<reason>)`. It is reported as that document's failure, and the run carries on.

**Measurement.** `scripts/ingest.py --dry-run` reports chunk quality per document and for the corpus (size distribution, oversized, empty, undersized, duplicates, chunks that start mid-sentence or end mid-word, chunks that are mostly a URL), writes every chunk to `chunked/` for inspection, and can record the report as JSON; it needs no database and no API keys. `scripts/evaluate.py` measures retrieval itself — recall@k and MRR over `evaluation/questions.yaml`, through the same `Retriever` the API uses — and can record a baseline and fail on a regression against it. `--check-dataset` tells a typo in the evaluation set apart from a retrieval failure by re-chunking the corpus and reporting the cases no chunk can satisfy.

**Query:** Client → /api/query (key validated, use recorded, rate limit enforced) → question
embedded → vector similarity search (+ filters) **and** typo-tolerant keyword search → results
merged (reciprocal rank fusion) → top-K chunks fetched from Postgres → prompt assembled → LLM call
→ output checks on the answer, excerpts and source names → query logged → answer + citations
returned

Everything before "question embedded" resolves as a FastAPI dependency, so an unauthenticated or
over-limit request never reaches the expensive part, and never learns what the payload should look
like.

## 7. Security Considerations
- API keys are 32 random bytes behind a `rag_` prefix, stored as SHA-256 only and never in plaintext; verified by hash lookup plus `hmac.compare_digest`, and revoked via `is_active` rather than deleted, so a key that was used can still be accounted for
- Per-key rate limiting, counted in Postgres so several workers share one allowance (§3.1)
- The internal admin surface has its own token in its own header and is not mounted without it; a consumer's API key can never reach it
- All credentials via environment variables only
- Input validation/size limits on /api/query before reaching the LLM call
- Retrieved text is untrusted data: passages and their source names are escaped and delimited (`generation/prompt.py`), so a chunk cannot close `<context>`, open a `<question>`, or forge an attribute on its passage, and the system prompt says to ignore instructions inside it. That makes the prompt's *structure* unforgeable; it does not stop a model from following an instruction written in a passage, which no prompt can guarantee. What limits the damage is that the model has no tools (`design-decisions.md`) and that every answer, excerpt and source name passes the output checks in `safety/output.py` before it leaves the service (§5)
- Timeouts everywhere a call leaves the process: the provider SDKs' own (bounded retries, from `models.*`), and a Postgres `statement_timeout` covering the vector search, the keyword search and every repository read. A query that would never return costs a 503, not a worker. Statements are deliberately not retried inside a request — a blind retry of a possible write is worse than the failure it hides — and `pool_pre_ping` covers the one case worth retrying transparently

### Logging: structured, and unable to leak a secret
`observability.py` formats logs as text or, in production, as JSON — one object per line, with
anything passed as `extra` becoming an indexed field. Redaction happens **in the formatter**
rather than at the call site, and that placement is the guarantee: a filter or a careful caller
can be forgotten, but every record passes through a formatter, including records from libraries
this project does not own and including exception tracebacks. It redacts the shapes a secret takes
here (an issued `rag_` key, a provider key, a password inside a connection URL, a bearer token)
and this deployment's own secret values once settings are loaded, which catches a password that
happens to look like an ordinary word.

The per-request line (`api/middleware.py`) names the key by **id**, never by value, and does not
include the question: a credential belongs nowhere but the caller's configuration, and the
question is user data that belongs in `query_logs`, where `api.log_queries` can switch it off.
Each response carries an `X-Request-ID` that an operator can search the log for.

### CORS: off by default, and deliberately so
A consumer frontend calls this API from its **own server**, where CORS does not apply and where an
API key can be held safely. A browser calling this API directly would have to hold that key
itself, where any visitor can read it and spend the client's LLM budget — so the browser case is
not the normal one to support, it is the one to avoid.

`api.cors.allowed_origins` is therefore empty by default and the middleware is not installed at
all when it is; an entry is a deliberate exception for a deployment that has accepted the
consequence. `*` is refused outright — every origin allowed here is one that could be made to
carry a key — as is an origin without a scheme. Allowed methods and headers are narrowed to what
this API has. Server-to-server callers are unaffected either way.

### Retrieval: meaning plus spelling
Vector search matches meaning but is weak on exact identifiers (a star like "VX Sagittarii" has almost no meaning to embed). So `retrieval/retriever.py` also runs a fuzzy keyword search (`ChunkRepository.fuzzy_search`, pg_trgm `word_similarity`): every 1-3 word phrase of the question (stop-words never start or end a phrase) is compared, by letter trigrams, against chunk text, so misspellings in the question or in the data still match. A phrase matching more than `fuzzy_max_matches_per_phrase` chunks is ignored as too common; chunks are ranked by total evidence (similarity x phrase length summed over phrases), and hits below `fuzzy_min_relative_evidence` of the best are dropped. Vector hits must still reach `similarity_threshold`; fuzzy hits must reach `fuzzy_min_similarity`. The two ranked lists are merged with reciprocal rank fusion, and tied or close chunks are all passed to the LLM, which picks the answer from them.


## 8. Extensibility and Deployment

**One codebase, N deployments.** Each client company runs this stack once, with its own database,
corpus, `config.yaml`, secrets and API keys; isolation is physical rather than a query filter
somebody has to remember to write. The reasoning, and the conditions that would justify revisiting
it, are in *Considered and not adopted* in `design-decisions.md`.

**The seams, in the order to reach for them.** A client need is answered at the first of these
that fits, and reaching past one that would have worked is how a codebase acquires a fork:

| Need | Answer |
|---|---|
| different sizes, models, thresholds, limits, documents | that client's `config.yaml` |
| a file format nobody has a reader for | a `Reader` — `adding-a-reader.md` |
| content somewhere the shared code cannot enumerate | an `Extractor` — `adding-an-extractor.md` |
| a different LLM, embedding model or vector store | an implementation behind the existing Protocol, plus a name in that config choice |
| any of the above, but only one client has it | the same thing, in `customisations/<client>/` |
| a client-specific *chunking* rule | none of the above: the element model is missing a field or a kind. Extend `extraction/base.py`, where every client benefits |

**The customisation boundary** (`customisations/`). `customisation: <name>` in config loads
`customisations/<name>`, which exposes one `Customisation(readers=..., extractors=...)` object.
Client readers are registered *ahead of* the built-in ones, so a client whose `.md` files are not
Markdown can say so. A name that does not resolve stops start-up rather than falling back to
none — a client's reader silently not loading would mean their documents were ingested by the
plain-text fallback, which looks like working software and returns wrong answers.

This is also why `extraction.provider` is not a closed `Literal`: which providers exist depends on
a module `settings.py` deliberately does not import, so an unknown name is rejected in
`extraction/factory.py` when the extractor is built — the start of a run — naming the ones
available. Every other provider choice *is* closed, and lists only what is implemented.

What the boundary refuses is the point. Not a fork of a pipeline stage; not a second place for
configuration; not a place for secrets. The failure it exists to prevent is five copies of the
pipeline in which one bug must be found and fixed five times, differently.

**Process model.** Gunicorn supervises Uvicorn workers: it restarts one that dies and reaps one
that hangs, which a bare `uvicorn` process does not. Concurrency comes from the event loop, not
from the worker count, so the usual `2 × cores + 1` rule for sync workers does not apply — workers
exist for CPU parallelism and crash isolation, and each holds its own database pool and provider
clients. `preload_app` is off, because the lifespan opens those pools per worker and preloading
would fork them into every child as shared file descriptors.

**Migrations run as their own step**, before the API starts, never on container start: every
worker would race the others, and with more than one replica the schema would be applied by
whichever container booted first. `deploy/README.md` has the commands;
`context/client-onboarding.md` is the runbook for standing a client up.
