# Development Workflow

## Approach

Build incrementally. The context files define what to build, how to build it, and why the system
is shaped the way it is. Always implement against these context files; do not infer or invent
behavior from scratch.

## Scoping Rules

- Work on one feature unit or subsystem at a time.
- Prefer small, verifiable increments over large speculative changes.
- Do not combine unrelated system boundaries in a single implementation step.

## When To Split Work

Split an implementation step if it combines:

- A pipeline stage change and an API change (extraction, chunking, embedding, vector storage,
  retrieval and generation are independent and are changed independently)
- A schema migration and the code that depends on it being applied
- Multiple unrelated API routes
- Behavior that is not clearly defined in the context files

If a change cannot be verified end to end quickly, the scope is too broad — split it.

## Handling Missing Requirements

- Do not invent product behavior that is not defined in the context files.
- If a requirement is ambiguous, resolve it in the relevant context file before implementing.
- If a requirement is missing, write it into the context file that owns it before continuing —
  `project-overview.md` for scope, `architecture-context.md` for structure, `design-decisions.md`
  for a choice between alternatives.

## Measurement, Not Opinion

Chunking and retrieval changes are measured, never argued. Record a baseline first
(`scripts/ingest.py --dry-run --qa-json ...` and `scripts/evaluate.py --save-baseline ...`),
change one thing, and keep it only if recall@k or MRR improved, or it fixed a defect without
regressing them. `chunking-guide.md` is the guide; `evaluation/baselines/README.md` says where the
baselines live and why none ships with the template.

## Do Not Modify Without Cause

- Alembic migrations that have already been applied anywhere. Schema changes are new migrations.
- Third-party library internals and vendored code.
- `docs/history/build-log.md`, which is a record of how the template was built and is not updated
  as your project progresses.

## Keeping Docs In Sync

Update the relevant context file whenever implementation changes:

- System architecture or boundaries → `architecture-context.md`
- Storage model decisions → `architecture-context.md` §3
- A choice made between real alternatives → `design-decisions.md`
- Code conventions or standards → `code-standards.md`
- Feature scope → `project-overview.md`

Documentation must reflect the actual state of the implementation, not the intended state.

## Before Moving To The Next Unit

1. The current unit works end to end within its defined scope.
2. No invariant defined in `architecture-context.md` was violated.
3. `ruff check .`, `mypy .` and `pytest` all pass.
