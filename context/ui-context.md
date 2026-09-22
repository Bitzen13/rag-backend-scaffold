# UI Context

## No UI

This project has no user interface. It is an API-only backend (see `project-overview.md` and `architecture-context.md`). It renders no pages, ships no frontend assets, and has no theme, typography, or component conventions. Consumer applications (a Next.js frontend, a Slack bot, a mobile app) live in separate repositories and own their own UI.

Do not add frontend frameworks, templates, static assets, or styling to this repo.

## Developer-Facing Surfaces

The only human-facing surfaces are for developers and operators:

- **API docs:** FastAPI's auto-generated OpenAPI docs (Swagger UI at `/docs`, ReDoc at `/redoc`). These are the interactive way to explore and test the API, so keep the schema accurate (`response_model`, error responses, summaries). Consider disabling or protecting them in production. The internal admin routes are deliberately absent from this document: a consumer reading it should not learn that an operations surface exists.
- **Internal admin routes:** an operator surface, not a UI — JSON over HTTP at `/internal`, for triggering ingestion and managing keys from somewhere without a shell on the box. Not mounted unless `ADMIN_API_TOKEN` is set.
- **CLI/scripts:** `scripts/ingest.py`, `scripts/reset.py` and `scripts/create_api_key.py` communicate through terminal output. Keep output concise and actionable: print what was ingested, skipped, or failed, and print a newly issued API key exactly once, with a reminder that it cannot be retrieved later. A destructive script shows what it will remove and asks for confirmation before doing it (`--dry-run` to see it, `--yes` to skip the prompt).
- **Logs:** structured, leveled logs via `logging` — text for a terminal, JSON for production (`logging.format`), with `extra` fields carrying the structure. Never log secrets, plaintext API keys, or full prompts containing user data at default log levels; `observability.py` redacts at the formatter as a backstop, but the first line of defence is not logging it. One line per request, naming the key by id and carrying the `X-Request-ID` that is also returned to the caller.

## Consumer Contract

Anything a consumer needs to render is carried by the API response shape, not by this repo:

- `POST /api/query` returns `answer` plus `sources` (`chunk_id`, `source_uri`, `excerpt`) so a client can show citations.
- The "no relevant context" case is a normal 200 response with a free-text answer and an empty `sources` list, so clients can display it like any other answer.
- Rate-limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After` on a 429) are on every response, so a client can show or respect a limit without guessing at one.
- The request/response shape is a versioned contract. Any change must be backward-compatible or versioned (see `architecture-context.md`).
