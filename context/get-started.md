# README — RAG Backend (Get Started)

## Prerequisites
- Python 3.11+
- Docker + Docker Compose (for local Postgres/vector DB)
- An API key for your chosen LLM provider (Anthropic/OpenAI)
- An API key for your chosen embedding provider (if different from the LLM provider)
- Optional, only for OCR of scanned PDF pages: the Tesseract binary on PATH, plus `pip install pytesseract`

## Steps

1. **Create your project from this template**
   Use "Use this template" (or clone + repoint the remote) to create a new repository for this specific project. Don't work directly in the template repo.

2. **Install dependencies**
   pip install -r requirements.txt
   (For development tooling — ruff, mypy, pytest — use `pip install -r requirements-dev.txt` instead.)

3. **Set up environment variables**
   Copy .env.example to .env and fill in:
   ANTHROPIC_API_KEY=...
   OPENAI_API_KEY=...
   DATABASE_URL=postgresql://...
   VECTOR_DB_URL=...

4. **Start local infrastructure**
   docker-compose up -d
   This spins up Postgres (and pgvector, if that's your configured vector DB).

5. **Run database migrations**
   python -m alembic upgrade head
   (Run from the repo root. Use `python -m alembic`, not the bare `alembic` command, which cannot import the project's modules.)
   (Reads `DATABASE_URL`; a plain `postgresql://` URL is fine, the async driver is applied automatically.)

6. **Configure the pipeline**
   Edit config.yaml to set chunk size/overlap (in tokens), top_k, and model choices. Every document to ingest must be listed under `chunking.documents` with its strategy (`fixed_size` or `document_aware`), optionally with per-document sizes. `context/chunking-guide.md` is the decision guide.

7. **Configure the extraction module**
   Set `extraction.path` (default: the `data/sample` documents) and `extraction.extensions`. A reader is chosen per extension — PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML, email, source code, and plain text for anything else — so most formats need no code. To add one, see `context/adding-a-reader.md`.

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
   The first run after upgrading to this version opens every source once: existing rows have no
   raw hash yet, and the recorded plan did not yet name a reader. Sources whose extracted text is
   unchanged are not re-embedded, so that pass costs parsing time but no embedding bill.

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
    Reports recall@k and MRR over the questions in `evaluation/questions.yaml` — replace those with questions about your own documents. After changing chunking or retrieval, re-run with `--baseline evaluation/baselines/retrieval.json`, which exits non-zero if a metric regressed.

12. **Issue an API key for your frontend/consumer**
    python scripts/create_api_key.py --label "my-frontend"
    Save the generated key — you'll need it in the frontend's .env.

13. **Run the server**
    uvicorn main:app --reload

14. **Test it**
    curl -X POST http://localhost:8000/api/query \
      -H "X-API-Key: <your-key>" \
      -H "Content-Type: application/json" \
      -d '{"question": "What is this about?"}'
    You should get back an answer with source references.
