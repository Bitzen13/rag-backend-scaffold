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
- **Be safe to run in a real environment.** Credentials should never be hardcoded, and API access should be controllable per consumer.
- **Be quickly verifiable.** The API should be testable directly (e.g. via curl or an API client) without requiring any frontend to exist.

## Feature list

**Ingestion & Extraction**
- Pluggable extraction layer converting raw source content into clean text
- Metadata tracking per ingested item (source identifier, ingestion timestamp, content hash/version)
- Change detection comparing current source state against tracked metadata
- Re-sync capability that reprocesses only changed or new content

**Chunking**
- Configurable chunking strategy (fixed-size, overlap, or semantic), controlled via config
- Chunk-level metadata preservation for traceability back to source

**Embedding & Vector Storage**
- Embedding generation via a configurable embedding model
- Vector + metadata storage in a vector database
- Update/removal of vectors tied to re-synced or deleted content

**Retrieval**
- Similarity search against the vector database
- Configurable retrieval depth and similarity thresholding
- Optional metadata filtering (source, date range, document type)

**Generation**
- Prompt construction combining retrieved context with the user's question
- Prompt template constraining the LLM to answer only from context, with an explicit "no relevant context" fallback
- Configurable LLM provider/model

**API**
- POST /api/query — accepts a question, returns an answer plus source references
- API-key-based authentication, with per-key issuance/revocation
- Rate limiting per API key
- OpenAPI-documented, so any consumer can generate a client or integrate without reading source code

**Configuration**
- Single configuration file controlling chunking, retrieval, and model parameters
- Environment-variable-based credential handling

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
A developer adjusts chunk size or retrieval count in the config file and re-runs ingestion/testing directly against the API, with no frontend involved at all.

**6. Handling a question with no relevant context**
Retrieval returns nothing above the similarity threshold; the fallback instruction in the prompt template causes the LLM to state it has no relevant information, rather than fabricating an answer.