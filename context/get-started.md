# README — RAG Backend (Get Started)

## Prerequisites
- Python 3.11+
- Docker + Docker Compose (for local Postgres/vector DB)
- An API key for your chosen LLM provider (Anthropic/OpenAI)
- An API key for your chosen embedding provider (if different from the LLM provider)
- Optional, only for OCR of scanned PDF pages: the Tesseract binary on PATH, plus `pip install pytesseract`

## Steps

1. **Create your project from this template**
   Use "Use this template" (or clone + repoint the remote) to create a new repository for this
   specific project. Don't work directly in the template repo.
   The sample corpus in `data/sample` is two short documents about RAG itself, there only so that
   step 8 works from a clean clone. Replace it, `chunking.documents` in `config.yaml`, and
   `evaluation/questions.yaml` with your own before building anything on top — `README.md` has
   the full list of what to change first.

2. **Install dependencies**
   pip install -r requirements.txt
   (For development tooling — ruff, mypy, pytest — use `pip install -r requirements-dev.txt` instead.)

3. **Set up environment variables**
   Copy .env.example to .env and fill in:
   ANTHROPIC_API_KEY=...
   OPENAI_API_KEY=...
   DATABASE_URL=postgresql://...
   VECTOR_DB_URL=...
   ADMIN_API_TOKEN=...   (optional; see step 15. Leave it unset and the internal admin
                          routes are not mounted at all, which is the default.)

4. **Start local infrastructure**
   docker-compose up -d
   This spins up Postgres (and pgvector, if that's your configured vector DB).

5. **Run database migrations**
   python -m alembic upgrade head
   (Run from the repo root. Use `python -m alembic`, not the bare `alembic` command, which cannot import the project's modules.)
   (Reads `DATABASE_URL`; a plain `postgresql://` URL is fine, the async driver is applied automatically.)

6. **Configure the pipeline**
   Edit config.yaml to set chunk size/overlap (in tokens), top_k, and model choices. The
   `api`, `database` and `logging` sections carry the operational settings — the per-key rate
   limit, whether questions are stored in `query_logs`, the timeouts on provider and database
   calls, and `logging.format: json` for production. All of them have working defaults. Every document to ingest must be listed under `chunking.documents` with its strategy (`fixed_size` or `document_aware`), optionally with per-document sizes. `context/chunking-guide.md` is the decision guide.

7. **Configure the extraction module**
   Set `extraction.path` (default: the `data/sample` documents) and `extraction.extensions`. A reader is chosen per extension — PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML, email, source code, and plain text for anything else — so most formats need no code. To add one, see `context/adding-a-reader.md`; for a data source other than a directory of files, `context/adding-an-extractor.md`. If the format or the source belongs to one client only, it goes in `customisations/` and is named with `customisation:` in their config — see `customisations/README.md`.

8. **Check the chunks before storing anything**
   python scripts/ingest.py --dry-run
   Prints a quality report (sizes, oversized or empty chunks, duplicates, suspicious cuts) and writes every chunk to `chunked/` for inspection. Needs no database and no API keys. Add `--qa-json evaluation/baselines/chunk-qa.json` to record it as a baseline before changing chunking settings.

9. **Ingest your data**
   python scripts/ingest.py            (or: --path some/other/dir)
   Re-running it is cheap and is how you re-sync. Every run hashes each file's raw bytes and
   opens only what is new, changed, or recorded against different chunking settings or a
   different reader version; a file that has gone is marked `deleted` and its chunks and vectors
   go with it. The run prints what it added, updated, skipped and removed, and how many sources
   it never had to open.

10. **Start over if you need to**
    python scripts/reset.py --dry-run           (list what is stored, delete nothing)
    python scripts/reset.py                     (delete every source, after confirmation)
    python scripts/reset.py --source notes.md   (just one document; repeatable)
    python scripts/reset.py --keep-chunked      (leave the chunked/ output alone)
    You rarely need this to re-chunk: changing a document's settings under `chunking.documents`
    (or bumping a reader's `version`) already re-chunks it on the next run. Reset is for starting
    from scratch. Deleting a source removes its chunks and vectors with it, in one transaction. The matching `chunked/*.chunks.txt` dry-run output is removed too, unless
    you pass `--keep-chunked`. API keys and query logs are untouched, and the schema stays
    put. Re-ingesting re-embeds, so it costs another run of the embedding bill.

11. **Measure retrieval, and record a baseline**
    python scripts/evaluate.py --check-dataset          (no database needed)
    python scripts/evaluate.py --save-baseline evaluation/baselines/retrieval.json
    Reports recall@k and MRR over the questions in `evaluation/questions.yaml`. The shipped
    questions are written against the two sample documents and measure nothing useful — replace
    them with 20-30 questions about your own documents first, then record the baseline. No
    baseline ships with the template on purpose (`evaluation/baselines/README.md` says why).
    After changing chunking or retrieval, re-run with `--baseline
    evaluation/baselines/retrieval.json`, which exits non-zero if a metric regressed.

12. **Issue an API key for your frontend/consumer**
    python scripts/create_api_key.py --label "my-frontend"
    Save the generated key: only its hash is stored, so this is the only time it can be shown, and
    you'll need it in the consumer's .env. If it is lost, revoke it and issue another.
    python scripts/create_api_key.py --list                 (every key, with its state)
    python scripts/create_api_key.py --revoke <id>          (deactivate one; --yes skips the prompt)
    Revocation takes effect on the next request, and the row is kept rather than deleted.

13. **Run the server**
    uvicorn main:app --reload
    Interactive docs at http://localhost:8000/docs — paste a key into "Authorize" and try the route
    from the browser. Consider protecting or disabling /docs and /redoc in production.

14. **Test it**
    curl -X POST http://localhost:8000/api/query \
      -H "X-API-Key: <your-key>" \
      -H "Content-Type: application/json" \
      -d '{"question": "What is this about?"}'
    You get back `{"answer": ..., "sources": [...]}`, one source per retrieved chunk (`chunk_id`,
    `source_uri`, `excerpt`). Narrow the search with
    `{"question": "...", "filters": {"source_type": "file"}}`.
    Ask something your documents do not cover and you still get a 200, with an answer saying so and
    an empty `sources` list — that is not an error. Errors are always `{"error": ..., "code": ...}`:
    401 `missing_api_key` or `invalid_api_key`, 400 `invalid_request` (malformed body, blank
    question, or one longer than `api.max_question_chars`), 429 `rate_limit_exceeded` when the key
    has spent its allowance for the current window, 503 `upstream_unavailable` when the LLM,
    embedding provider or database cannot be reached.
    Every response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining` (and `Retry-After` on a
    429), so a consumer can pace itself rather than wait to be refused. Tune or disable the limit
    under `api.rate_limit` in config.yaml; `requests: 0` turns it off.

15. **Optional: the internal admin routes**
    Set `ADMIN_API_TOKEN` in .env and restart. Without it these routes are not mounted at all, so
    a deployment that does not want an operations surface does not have one. They are absent from
    /docs, and a consumer's API key cannot reach them — they need the token in `X-Admin-Token`:

    curl -X POST http://localhost:8000/internal/ingest -H "X-Admin-Token: $ADMIN_API_TOKEN"
    Starts a run (re-syncing is the same operation) and returns 202 with a job id, because a run
    takes minutes. Poll it with `GET /internal/ingest/<id>`, or list recent runs with
    `GET /internal/ingest`. Only one run happens at a time; a second request gets 409. If a worker
    dies mid-run its job stays `running` — `DELETE /internal/ingest/<id>` releases the slot.

    curl http://localhost:8000/internal/api-keys -H "X-Admin-Token: $ADMIN_API_TOKEN"
    Lists, issues (`POST` with `{"label": "..."}`, which returns the plaintext key once) and
    revokes (`DELETE /internal/api-keys/<id>`) keys — the same operations as
    `scripts/create_api_key.py`, for when you have no shell on the box.

    Put a network boundary in front of these as well. The token is the second lock, not the only
    one.

16. **Logs**
    One structured line per request: method, path, status, duration, the key's *id*, and an
    `X-Request-ID` that is also returned to the caller, so a request a consumer reports can be
    found. Questions are not in that line — they go to `query_logs` when `api.log_queries` is on,
    and nowhere when it is off. Anything that looks like a secret is redacted before a line is
    written, whoever logged it. Set `logging.format: json` in production so a log shipper can index
    the fields instead of parsing prose.

## Deploying it

Local development is the steps above. Production is a different document, because it is a
different shape: one image, and one stack per client company with its own database, corpus,
`config.yaml` and secrets.

```bash
docker build -t rag-backend:1.0.0 .
```

- `deploy/README.md` — the mechanics: building, the Compose template, why migrations are their own
  step, the process model, and the checklist before it faces the internet.
- `context/client-onboarding.md` — the runbook for a new client, in order: provision, corpus,
  configure, check the chunks, ingest, record both baselines, issue the key, verify, hand over.

## The context documents

| Document | When |
|---|---|
| `project-overview.md` | what this is and what it is for |
| `architecture-context.md` | structure, boundaries, storage model, invariants |
| `code-standards.md` | the conventions to implement against |
| `design-decisions.md` | why the system is shaped this way, and what was deliberately not built |
| `chunking-guide.md` | which chunking strategy suits which content, and how to measure a change |
| `adding-a-reader.md` | supporting a new file format |
| `adding-an-extractor.md` | supporting a new data source |
| `client-onboarding.md` | standing up a new client |
| `ui-context.md` | there is no UI; the developer-facing surfaces |
| `ai-workflow-rules.md` | how to work on this incrementally |
| `customisations/README.md` | where one client's code lives, and what must not go there |
| `deploy/README.md` | deploying |
