# Project Overview — RAG Backend

## Overview

This system is the backend for a Retrieval-Augmented Generation (RAG) chatbot. It accepts a user's natural-language question, retrieves the most relevant pieces of context from a vector database, constructs a grounded prompt combining that context with the question, and calls a large language model (LLM) to generate an answer. It is a standalone, API-only service — it has no UI of its own — and is designed to be consumed by any client application (a Next.js frontend, a Slack bot, a mobile app, etc.) over a REST API secured with API-key authentication.

The architecture is deliberately modular. Each stage of the pipeline (extraction, chunking, embedding, vector storage, retrieval, and generation) is an independent, swappable component. This means the system can be pointed at a new data source primarily by replacing the extraction layer — the module responsible for pulling raw text out of whatever source format is in play (documents, web pages, databases, APIs, etc.) — without needing to rewrite the retrieval or generation logic. Tunable parameters such as chunk size, number of retrieved chunks, and model selection live in a single configuration file, so behavior can be adjusted without touching code.

Beyond the core question-answering loop, the system includes the infrastructure needed to run reliably over time: a metadata layer that tracks what has been ingested, a change-detection mechanism that identifies when source documents have been updated and need re-syncing, environment-variable-based credential management so secrets never live in source code, and a prompt template that explicitly constrains the LLM to answer only from retrieved context.

## Goals

- **Answer accurately, grounded in real source material.** The LLM should never be answering from unconstrained general knowledge when domain-specific context is available and relevant.
- **Keep the pipeline modular and independently testable.** Each stage should be swappable and testable in isolation.
- **Be entirely UI-agnostic.** The system exposes functionality only through an authenticated API — it makes no assumptions about, and has no dependency on, what consumes it.
- **Make configuration the primary lever for tuning.** Chunk size, retrieval depth, model choice, and similar parameters should be adjustable from a single config surface.
- **Stay in sync with changing source data.** The system should detect when underlying documents have changed and re-ingest only what's needed.
- **Serve each client company from its own deployment.** One instance, one database and one corpus per client, so isolation is physical rather than enforced by a query filter, and a client's configuration, models and extraction layer can differ without affecting anyone else.
- **Be safe to run in a real environment.** Credentials should never be hardcoded, and API access should be controllable per consumer.
- **Be quickly verifiable.** The API should be testable directly (e.g. via curl or an API client) without requiring any frontend to exist.

## Feature list

**Ingestion & Extraction**
- Pluggable extraction layer converting raw source content into clean text, with a reader per format (PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML, email, source code) and plain text as the fallback, so an unfamiliar format is still ingested
- Metadata tracking per ingested item (source identifier, ingestion timestamp, content hash/version)
- Change detection comparing current source state against tracked metadata
- Re-sync capability that reprocesses only changed or new content

**Chunking**
- Chunking strategy chosen manually per document via config (fixed-size with overlap, or document-aware), with per-document size and splitting overrides
- One document-aware algorithm over an abstract element model, so a new format is a new reader and not a new strategy: a table row per chunk, code blocks whole, list items under their lead-in, a question with its answer, transcript turns by time window, a message per chunk, prose packed by section
- Sizes measured in the embedding model's own tokens, with a hard cap so no chunk is ever silently truncated
- Sentence-aware splitting of long prose, without assuming English
- Chunk-level metadata preservation for traceability back to source

**Embedding & Vector Storage**
- Embedding generation via a configurable embedding model
- Vector + metadata storage in a vector database
- Update/removal of vectors tied to re-synced or deleted content

**Retrieval**
- Similarity search against the vector database, merged with a typo-tolerant keyword search (character-trigram matching) so exact names and misspellings still find their chunks
- Configurable retrieval depth and similarity thresholding
- Optional metadata filtering (source, date range, document type)

**Generation**
- Prompt construction combining retrieved context with the user's question
- Conversational prompt template: greetings and small talk are answered naturally, while factual answers come only from retrieved context (the model says it lacks the information otherwise)
- Configurable LLM provider/model — Anthropic and OpenAI both implemented behind one interface, so a client's existing account is a config change

**API**
- POST /api/query — accepts a question, returns an answer plus source references
- API-key-based authentication, with per-key issuance/revocation
- Rate limiting per API key, counted in the database so several workers share one allowance rather than one each
- Query logging per answered question, switchable off for a deployment whose questions are sensitive
- Internal admin routes for triggering ingestion and managing keys, behind their own token and not mounted unless one is set
- OpenAPI-documented, so any consumer can generate a client or integrate without reading source code

**Configuration**
- Single configuration file controlling chunking, retrieval, and model parameters, validated on load so a setting that cannot take effect is an error rather than a silent no-op
- Timeouts and bounded retries on every call that leaves the process, so a slow dependency costs a 503 rather than a stuck request
- Environment-variable-based credential handling

**Operations**
- Structured logging (text or JSON) that redacts anything resembling a secret at the formatter, so no code path can leak one
- A request id per call, returned to the caller and logged, so a report of "it failed" is traceable
- A production image (non-root, multi-stage) with Gunicorn supervising Uvicorn workers, and migrations as their own deploy step rather than a start-up race
- One deployment per client from a single template: the same image, the client's own config, secrets, corpus and keys
- A customisation boundary so N deployments stay one codebase — a client's own format or data source lives behind the existing Protocols in a module the shared code does not own, never in a fork
- CORS off by default, because a consumer calls this API server-side and an API key must never reach a browser

**Measurement**
- Chunk quality report before anything is stored: size distribution, oversized or empty chunks, duplicates, cuts in the wrong place
- Retrieval evaluation harness reporting recall@k and MRR over a question set, with recorded baselines so a change to chunking or retrieval is kept only if it measurably helps

## Core user flows (examples)

**1. Ingesting a new data source**
A developer runs the ingestion CLI/script pointing at a new source. The system extracts, chunks, embeds, and stores the content, along with metadata for tracking.

**2. A consumer application asking a question**
Any authenticated client (regardless of what it's built in) sends POST /api/query with a question and its API key. The backend embeds the question, retrieves the top-K relevant chunks, constructs a grounded prompt, calls the LLM, and returns the answer with source references — with zero awareness of, or dependency on, what kind of application made the call.

**3. Re-syncing after a source document changes**
On the next ingestion run, change detection identifies the modified document via its content hash, removes stale chunks/vectors, and reprocesses only that document.

**4. Onboarding a new consumer application**
A new client (e.g., a newly built frontend, or a Slack bot) is issued its own API key via the key-management process, scoped and revocable independently of any other consumer's key.

**5. Tuning retrieval behavior**
A developer records a baseline (`scripts/ingest.py --dry-run --qa-json ...` and `scripts/evaluate.py --save-baseline ...`), adjusts chunk size, splitting mode or retrieval count in the config file, re-runs both, and keeps the change only if recall@k or MRR improved — no frontend involved at all.

**6. Ingesting a format nobody planned for**
A directory turns out to contain .eml exports and a spreadsheet alongside the PDFs. Each goes to its own reader, and a format with no reader is read as plain text with light structure detection, so the run completes rather than skipping files.

**7. Trying to outrun the rate limit**
A consumer loops on /api/query. Its allowance for the window runs out and further calls get 429
with `Retry-After`, before any embedding or LLM call is made. Other consumers' keys are unaffected,
because the allowance is per key.

**8. Handling a question with no relevant context**
Retrieval returns nothing above the similarity threshold; the LLM is still called with an empty context. A greeting or small talk gets a natural reply; a factual question gets a statement that the information is not in the knowledge base, rather than a fabricated answer.
