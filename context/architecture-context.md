# Technical Architecture Document — RAG Backend

## 1. Tech Stack

- **Language/Framework:** Python, FastAPI (async, OpenAPI docs generated automatically)
- **ASGI server:** Uvicorn (Gunicorn+Uvicorn workers in production)
- **LLM provider:** Configurable — Anthropic or OpenAI, abstracted behind a provider interface
- **Embedding model:** Configurable — e.g. OpenAI text-embedding-3-small/large, or local sentence-transformers
- **Vector database:** Configurable — pgvector (default, avoids a separate service), Pinecone, Qdrant, or Weaviate
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
- End-user identity/auth (only the calling application is authenticated, via API key)
- Long-term conversational memory across requests (each call is stateless unless the caller passes history in the payload)
- Canonical storage of original source documents (only derived/extracted data is stored)
- Model fine-tuning/training

**External boundaries**
- Inbound: HTTP requests to /api/query from any authenticated consumer (frontend, Slack bot, mobile app)
- Inbound (ops-only): ingestion triggered via CLI/internal admin endpoint, not publicly exposed
- Outbound: LLM provider API, embedding provider API, vector database, Postgres

## 3. Database Structures

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

**query_logs** (optional)
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | |
| api_key_id | UUID (FK) | |
| question | text | |
| answer | text | |
| retrieved_chunk_ids | UUID[] | |
| created_at | timestamptz | |

### 3.2 Vector database structure
Each record: chunk_id (join key to Postgres), embedding (float array), and a metadata payload (source_id, source_type, chunk_index, tags/date for filtering). Full chunk text lives in Postgres, not only in the vector DB payload, to avoid provider payload-size limits and simplify re-embedding.

**pgvector implementation (default):** table `chunk_vectors` — `chunk_id` (UUID PK, FK → `chunks.id` ON DELETE CASCADE), `embedding` (`vector(1536)`), `source_id` (FK → `sources.id` ON DELETE CASCADE), `source_type`, `chunk_index`, `metadata` (jsonb). Indexed with HNSW using cosine distance (`vector_cosine_ops`); search returns cosine similarity (`1 - distance`). The column width is fixed by the migration (`EMBEDDING_DIMENSIONS` in `db/models.py`, 1536 for `text-embedding-3-small`); switching to an embedding model with a different size requires a new migration that rebuilds `chunk_vectors`. The store rejects vectors of the wrong size.

**Data-access conventions:** repositories (`db/repositories/`) and `PgVectorStore` are bound to an `AsyncSession`, flush but never commit — the caller owns the transaction, so Postgres rows and vectors can be written or removed atomically. The `VectorStore` Protocol lives in `vector_store/base.py`; similarity thresholding is applied by the retrieval layer, not the store.

## 4. Configuration Structure

extraction:
  provider: filesystem
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

chunking:
  chunk_size: 500                      # sizes are in TOKENS of the embedding model below
  chunk_overlap: 50
  max_chunk_tokens: 7781               # hard cap; unset = derived from the model's input limit
  tokenizer: auto                      # auto (the embedding model's own) | words
  prose_splitting: sentence            # paragraph | sentence
  documents:            # strategy chosen manually for EACH document; unlisted = not ingested
    rag-overview.md: document_aware      # fixed_size | document_aware
    List_of_largest_stars.pdf: document_aware
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

vector_db:
  provider: pgvector | pinecone | qdrant | weaviate

auth:
  api_key_header: X-API-Key

Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY, DATABASE_URL, VECTOR_DB_URL) come exclusively from environment variables.

Validation at load time, so a bad value fails loudly rather than producing bad chunks: every effective `chunk_overlap` is below its `chunk_size`, every `chunk_size` is within the hard cap, the hard cap is within the embedding model's input limit, `prose_splitting` is only set on `document_aware`, and an embedding model whose input limit is not known requires `max_chunk_tokens` to be set explicitly.

## 5. API Design

**POST /api/query**
- **Auth:** X-API-Key header, validated against api_keys.key_hash
- **Request:** { "question": "string", "filters": { "source_type": "optional" } }
- **Response:** { "answer": "string", "sources": [{ "chunk_id": "uuid", "source_uri": "string", "excerpt": "string" }] }
- **Errors:** 401 invalid/missing key, 400 malformed request, 503 upstream (LLM/vector DB) unreachable, an empty retrieval is not an error: the LLM is still called, with an empty context, and the response is a normal 200 (with no sources)

This request/response shape is the contract the frontend template (and any other consumer) is built against — changes to it must be versioned or made backward-compatible, since consumers are now maintained in separate repos.

**Internal/admin (not public):** trigger ingestion, trigger re-sync, issue/revoke API keys.

## 6. Data Flow Summary

**Ingestion:** Candidates (raw hashes, no parsing) → what changed? → Extractor → Chunker → Embedder → Vector DB (+ sources/chunks rows in Postgres, both hashes updated); sources that have gone are marked `deleted` and their chunks and vectors removed

Implemented in `ingestion/pipeline.py`, one transaction per document.

**Extraction is a reader per format, all producing one element model.** `extraction/base.py` defines the elements — heading, paragraph, list item, table row, code, question, answer, speaker turn, message — each carrying the context a chunk built from it will need (heading path, table caption, list lead-in, speaker, language, page/sheet/slide, line range). `extraction/readers/registry.py` maps an extension to its reader: PDF, Markdown, HTML, DOCX, CSV/TSV, Excel, JSON/JSONL/YAML, email (eml/mbox), source code, and plain text as the fallback for any other extension, so an unknown format is still ingested rather than skipped. A reader that cannot read a file logs it and the run continues. `extraction/elements.py` assembles every reader's output, joining element texts into the document text and giving each element its offsets in it. See `adding-a-reader.md`.

**Chunking is one algorithm over that element model**, so a new format never changes it. `document_aware` (`chunking/document_aware.py`) applies a rule per element kind: a table row is one chunk with its caption and column names; a code block stays whole; list items group under their lead-in; a question becomes the context its answers repeat; transcript turns group into a time window; a message is one chunk; paragraphs pack together under their heading path. A chunk's context line is counted against its token budget, not added on top of it, and a heading path too deep to fit is trimmed from the outside in. `fixed_size` remains for text with no structure worth preserving, and `document_aware` falls back to it for a document with no elements. Only one element can ever be oversized within a group, which is what keeps each split part's offsets exact. See `chunking-guide.md`.

**Sizes are in tokens of the embedding model** (`chunking/tokenizer.py`, tiktoken; word counts as a fallback with a warning). `chunking/sentences.py` finds sentence boundaries without assuming English — Latin, CJK, Devanagari, Arabic, Urdu and Ethiopic terminators, abbreviations and initials excluded, falling back to one span for text with no sentence punctuation. `chunking/prose.py` packs words or whole sentences into a token budget, overlapping by whole sentences in `sentence` mode and splitting by character as a last resort for text with no word breaks. Every chunk passes through `chunking/assembly.py`, which counts it exactly and enforces `max_chunk_tokens`, so no chunk is ever handed to the embedding model to be truncated.

**The PDF reader** (`extraction/pdf/`) works structure out per document from that document's own statistics, not fixed values: headings are lines larger than the document's body font (or short bold lines) with levels ranked by size; a table's first row is its header unless it is mostly numeric; a table starting a page right after one that reached the bottom continues it (repeated header rows are dropped); the short line above a table is its caption; running headers/footers are removed; text beside a figure is kept as separate paragraphs; multi-column pages are read column by column; paragraphs are joined across page breaks. Tables aligned with whitespace rather than ruling lines are found by `extraction/pdf/borderless.py`, which is deliberately hard to trigger — the rows must agree on column count and position, hold short values, contain no sentence running through a cell, and not read on into one another — because a false positive turns prose into nonsense rows. A page holding an image of text instead of text can be read with OCR (`extraction/pdf/ocr.py`, opt-in, needs Tesseract), which produces words of the same shape as any other page and so goes through the same layout analysis. Corpus-specific cleanup (citation markers, unrendered template text, sections to skip) is configured under `extraction.pdf` as regular expressions. Not supported: OCR of handwriting, `.doc`/`.xls`, slide decks.

**Change detection decides before extraction, not after it.** `Extractor.candidates()` enumerates the sources that exist without parsing any of them: each `SourceCandidate` carries its `source_hash`, the reader that would handle it and that reader's version, and parses only when `read()` is called. A run loads the stored state for its `source_type` in one query and opens a document only when it is new, previously marked deleted, changed in its bytes, or recorded against a different plan or reader version. On the sample corpus, enumerating and hashing all nine files (40MB) takes **0.031s** where parsing them takes **69s** — the whole performance case for this shape, and worth about two thousandfold.

Documents are then processed one at a time and streamed: nothing beyond a single document's chunks is ever held in memory. A changed source has its chunks and vectors replaced in the same transaction. A source that is opened but turns out to produce identical text has its new `source_hash` recorded and is not re-embedded.

**Deletion comes for free from the candidate list**, which is exactly the set of sources that still exist. Anything stored, not already deleted, and missing from it is marked `deleted` and loses its chunks and vectors in one transaction.

**Measurement.** `scripts/ingest.py --dry-run` reports chunk quality per document and for the corpus (size distribution, oversized, empty, undersized, duplicates, chunks that start mid-sentence or end mid-word, chunks that are mostly a URL), writes every chunk to `chunked/` for inspection, and can record the report as JSON; it needs no database and no API keys. `scripts/evaluate.py` measures retrieval itself — recall@k and MRR over `evaluation/questions.yaml`, through the same `Retriever` the API uses — and can record a baseline and fail on a regression against it. `--check-dataset` tells a typo in the evaluation set apart from a retrieval failure by re-chunking the corpus and reporting the cases no chunk can satisfy.

**Query:** (Phase 4 implements everything after the API layer: `services/query_service.py` → `retrieval/retriever.py` + `generation/`.) Client → /api/query (key validated) → question embedded → vector similarity search (+ filters) **and** typo-tolerant keyword search → results merged (reciprocal rank fusion) → top-K chunks fetched from Postgres → prompt assembled → LLM call → answer + citations returned

## 7. Security Considerations
- API keys stored hashed, revoked via is_active rather than deleted
- Per-key rate limiting
- All credentials via environment variables only
- Input validation/size limits on /api/query before reaching the LLM call
- CORS: since consumers are now separate deployed applications (e.g., the Next.js frontend calling server-side), CORS is only relevant if a browser ever calls this API directly — server-to-server calls from Next.js Route Handlers aren't subject to CORS, but the backend should still allowlist expected server origins/IPs where feasible

### Retrieval: meaning plus spelling
Vector search matches meaning but is weak on exact identifiers (a star like "VX Sagittarii" has almost no meaning to embed). So `retrieval/retriever.py` also runs a fuzzy keyword search (`ChunkRepository.fuzzy_search`, pg_trgm `word_similarity`): every 1-3 word phrase of the question (stop-words never start or end a phrase) is compared, by letter trigrams, against chunk text, so misspellings in the question or in the data still match. A phrase matching more than `fuzzy_max_matches_per_phrase` chunks is ignored as too common; chunks are ranked by total evidence (similarity x phrase length summed over phrases), and hits below `fuzzy_min_relative_evidence` of the best are dropped. Vector hits must still reach `similarity_threshold`; fuzzy hits must reach `fuzzy_min_similarity`. The two ranked lists are merged with reciprocal rank fusion, and tied or close chunks are all passed to the LLM, which picks the answer from them.

