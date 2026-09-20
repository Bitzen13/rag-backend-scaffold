# Technical Architecture Document — RAG Backend

## 1. Tech Stack

- **Language/Framework:** Python, FastAPI (async, OpenAPI docs generated automatically)
- **ASGI server:** Uvicorn (Gunicorn+Uvicorn workers in production)
- **LLM provider:** Configurable — Anthropic or OpenAI, abstracted behind a provider interface
- **Embedding model:** Configurable — e.g. OpenAI text-embedding-3-small/large, or local sentence-transformers
- **Vector database:** Configurable — pgvector (default, avoids a separate service), Pinecone, Qdrant, or Weaviate
- **Relational database:** PostgreSQL — ingestion metadata, chunks, API keys, query logs
- **Containerization:** Docker + docker-compose for local dev
- **Secrets:** Environment variables only
- **Document parsing libraries:** unstructured, pypdf, beautifulsoup4 (source-dependent, isolated in the extraction module)
- **Chunking libraries:** custom splitters or langchain/llama-index splitters used as libraries only

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
| content_hash | text | SHA-256, drives change detection |
| last_ingested_at | timestamptz | |
| status | text | active / stale / deleted (enforced by a CHECK constraint) |
| metadata | jsonb | source-specific metadata |

Constraint: `UNIQUE (source_type, source_uri)` — a source is identified by its type and URI, which is what change detection looks up.

**chunks**
| Column | Type | Notes |
|---|---|---|
| id | UUID (PK) | matches vector DB chunk_id |
| source_id | UUID (FK) | |
| chunk_index | int | |
| text | text | retained for citation and re-embedding |
| token_count | int | |
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
  extensions: [.md, .txt]

chunking:
  strategy: fixed_size | semantic
  chunk_size: 500
  chunk_overlap: 50

retrieval:
  top_k: 5
  similarity_threshold: 0.75

models:
  embedding_provider: openai
  embedding_model: text-embedding-3-small
  llm_provider: anthropic
  llm_model: claude-sonnet-4-6

vector_db:
  provider: pgvector | pinecone | qdrant | weaviate

auth:
  api_key_header: X-API-Key

Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY, DATABASE_URL, VECTOR_DB_URL) come exclusively from environment variables.

## 5. API Design

**POST /api/query**
- **Auth:** X-API-Key header, validated against api_keys.key_hash
- **Request:** { "question": "string", "filters": { "source_type": "optional" } }
- **Response:** { "answer": "string", "sources": [{ "chunk_id": "uuid", "source_uri": "string", "excerpt": "string" }] }
- **Errors:** 401 invalid/missing key, 400 malformed request, 503 upstream (LLM/vector DB) unreachable, 200 with a "no relevant context" answer when retrieval is empty

This request/response shape is the contract the frontend template (and any other consumer) is built against — changes to it must be versioned or made backward-compatible, since consumers are now maintained in separate repos.

**Internal/admin (not public):** trigger ingestion, trigger re-sync, issue/revoke API keys.

## 6. Data Flow Summary

**Ingestion:** Source → Extractor → Chunker → Embedder → Vector DB (+ sources/chunks rows in Postgres, content_hash updated)

Implemented in `ingestion/pipeline.py`, one transaction per document. Chunk size/overlap are counted in whitespace-delimited words (a tokenizer-free approximation of tokens); only the `fixed_size` strategy is implemented. An unchanged source (same `content_hash`) is skipped; a changed one has its chunks and vectors replaced in the same transaction.

**Query:** Client → /api/query (key validated) → question embedded → vector similarity search (+ filters) → top-K chunks fetched from Postgres → prompt assembled → LLM call → answer + citations returned

## 7. Security Considerations
- API keys stored hashed, revoked via is_active rather than deleted
- Per-key rate limiting
- All credentials via environment variables only
- Input validation/size limits on /api/query before reaching the LLM call
- CORS: since consumers are now separate deployed applications (e.g., the Next.js frontend calling server-side), CORS is only relevant if a browser ever calls this API directly — server-to-server calls from Next.js Route Handlers aren't subject to CORS, but the backend should still allowlist expected server origins/IPs where feasible