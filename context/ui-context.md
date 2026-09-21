# UI Context

## No UI

This project has no user interface. It is an API-only backend (see `project-overview.md` and `architecture-context.md`). It renders no pages, ships no frontend assets, and has no theme, typography, or component conventions. Consumer applications (a Next.js frontend, a Slack bot, a mobile app) live in separate repositories and own their own UI.

Do not add frontend frameworks, templates, static assets, or styling to this repo.

## Developer-Facing Surfaces

The only human-facing surfaces are for developers and operators:

- **API docs:** FastAPI's auto-generated OpenAPI docs (Swagger UI at `/docs`, ReDoc at `/redoc`). These are the interactive way to explore and test the API, so keep the schema accurate (`response_model`, error responses, summaries). Consider disabling or protecting them in production.
- **CLI/scripts:** `scripts/ingest.py` and `scripts/create_api_key.py` communicate through terminal output. Keep output concise and actionable: print what was ingested, skipped, or failed, and print a newly issued API key exactly once, with a reminder that it cannot be retrieved later.
- **Logs:** structured, leveled logs via `logging`. Never log secrets, plaintext API keys, or full prompts containing user data at default log levels.

## Consumer Contract

Anything a consumer needs to render is carried by the API response shape, not by this repo:

- `POST /api/query` returns `answer` plus `sources` (`chunk_id`, `source_uri`, `excerpt`) so a client can show citations.
- The "no relevant context" case is a normal 200 response with a free-text answer and an empty `sources` list, so clients can display it like any other answer.
- The request/response shape is a versioned contract. Any change must be backward-compatible or versioned (see `architecture-context.md`).
