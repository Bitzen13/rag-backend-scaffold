# README — RAG Backend (Get Started)

## Prerequisites
- Python 3.11+
- Docker + Docker Compose (for local Postgres/vector DB)
- An API key for your chosen LLM provider (Anthropic/OpenAI)
- An API key for your chosen embedding provider (if different from the LLM provider)

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
   Edit config.yaml to set chunk size/overlap, top_k, and model choices. Every document to ingest must be listed under `chunking.documents` with its strategy (`fixed_size` or `document_aware`). Preview chunks first with `python scripts/ingest.py --dry-run` (writes to `chunked/`).

7. **Implement/configure the extraction module**
   Set `extraction.path` in config.yaml (default: the `data/sample` documents), or add a new extractor in extraction/.

8. **Ingest your data**
   python scripts/ingest.py            (or: --path some/other/dir)

9. **Issue an API key for your frontend/consumer**
   python scripts/create_api_key.py --label "my-frontend"
   Save the generated key — you'll need it in the frontend's .env.

10. **Run the server**
    uvicorn main:app --reload

11. **Test it**
    curl -X POST http://localhost:8000/api/query \
      -H "X-API-Key: <your-key>" \
      -H "Content-Type: application/json" \
      -d '{"question": "What is this about?"}'
    You should get back an answer with source references.