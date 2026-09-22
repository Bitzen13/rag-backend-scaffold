## Application Building Context

This repository is a **template**. A project started from it replaces the corpus, the config and
the evaluation set, and keeps the pipeline. `README.md` lists what to change first; do not treat
the sample corpus in `data/sample` or the shipped `evaluation/questions.yaml` as this project's
real content.

Read the following files in order before implementing or making any architectural decision:

1. `context/project-overview.md` — product definition, goals, features, and scope
2. `context/architecture-context.md` — system structure, boundaries, storage model, and invariants
3. `context/design-decisions.md` — why the system is shaped this way, and what was deliberately not built
4. `context/ui-context.md` — confirms there is no UI; lists developer-facing surfaces (API docs, CLI output, logs)
5. `context/code-standards.md` — implementation rules and conventions (Python, FastAPI, database, retrieval/generation, testing)
6. `context/ai-workflow-rules.md` — development workflow, scoping rules, and delivery approach
7. `context/get-started.md` — local setup and run steps

Then, when the work touches extraction or chunking:

8. `context/chunking-guide.md` — which chunking strategy and options suit which content, and how to measure a change
9. `context/adding-a-reader.md` — the element model, and how to support a new file format
10. `context/adding-an-extractor.md` — how to support a new data source (not a new format)

And when the work touches deployment or anything client-specific:

11. `deploy/README.md` — the production image, the per-client stack, and what must not be baked into it
12. `customisations/README.md` — where one client's code lives, and what must never go there
13. `context/client-onboarding.md` — the runbook for standing up a new client

`docs/history/build-log.md` records how the template itself was built, phase by phase. It is
history, not instruction: every phase in it is already done, and its figures describe a sample
corpus that projects delete. Read it for the reasoning, never as a task list.

If implementation changes the architecture, scope, or standards documented in the context files,
update the relevant file before continuing.

## Stack

Python 3.11+, FastAPI on Uvicorn, PostgreSQL (with pgvector by default), Alembic migrations, Docker Compose for local dev. LLM, embedding, and vector DB providers are configurable via `config.yaml` behind provider interfaces. Secrets come from environment variables only.

This is an API-only backend. Do not add UI code, frontend frameworks, or styling.

Always keep responses brief and concise. Implement the work you are given.
