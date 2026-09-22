# Client Onboarding Runbook

Standing up a new client company. Every step is configuration, documents or an operator command —
**no code change**. If you find yourself needing one, stop at step 4 and read
`customisations/README.md`: the answer is a module the shared codebase does not own, not a fork.

The mechanics of the deployment template are in `deploy/README.md`. This is the order to do things
in, and what to check before handing anything over.

Budget: an afternoon for a corpus that reads cleanly. Most of it is steps 5 and 7 — deciding the
chunking, and looking at what came out.

---

## 1. Provision

```bash
cp -r deploy/clients/example deploy/clients/acme
cp deploy/clients/acme/.env.example deploy/clients/acme/.env
```

Fill in `.env`: the provider key for the model they will use, a **generated** `POSTGRES_PASSWORD`
(not a shared default), and `API_PORT` if this host already runs another client. Leave
`ADMIN_API_TOKEN` blank unless they need the internal path — blank means `/internal` is not mounted
at all.

Pin the image: `IMAGE=rag-backend:1.0.0`. `latest` means a restart can change versions.

## 2. Collect the corpus

Put their documents in `deploy/clients/acme/documents/`. Get the **real** corpus, not a sample:
chunking decisions made against three tidy PDFs do not survive the other two hundred.

Ask two questions now, because both are expensive to discover later:

- **Is anything in here confidential beyond the obvious?** If questions themselves are sensitive,
  set `api.log_queries: false` in step 3.
- **How often does it change?** Re-ingestion is cheap for unchanged files (hashes, not parses) but
  a changed document is re-embedded, which costs. It decides how often ingestion runs, not whether
  it can.

## 3. Configure

Edit `deploy/clients/acme/config.yaml`. The parts that are genuinely per-client:

| Setting | Decide by |
|---|---|
| `extraction.extensions` | what they actually have. A format with no reader is read as plain text, not skipped |
| `extraction.pdf.*` | their documents' boilerplate — citation markers, headers, a References section. Leave empty until step 7 shows you need it |
| `chunking.documents` | **every document to ingest, by name.** Unlisted is not ingested |
| `models.*` | the provider they have an account with, and the model they are paying for |
| `retrieval.*` | leave at the defaults until step 8 measures something better |
| `api.rate_limit` | what their consumers are entitled to. `requests: 0` disables it |
| `api.log_queries` | `false` if their questions are sensitive |
| `logging.format` | `json` in production |

`chunking.documents` is the one that takes real time. `context/chunking-guide.md` is the decision
guide; default to `document_aware` and choose `fixed_size` deliberately, for a file that is
genuinely a wall of text. A document with no entry is not ingested and the run names it, so
nothing is silently missed.

## 4. Anything that needs code?

A format nobody has a reader for, or content somewhere the shared code cannot enumerate. Both have
a home that is not a fork:

- a format → a reader in their customisation module (`context/adding-a-reader.md`)
- a data source → an extractor in it (`context/adding-an-extractor.md`)

Then `customisation: acme` in their `config.yaml`. If neither fits, look again — usually the answer
is config, or a missing field on the element model, which belongs in the shared codebase where
every client gets it.

## 5. Check the chunks before storing anything

```bash
export CLIENT=acme
COMPOSE="docker compose -f deploy/docker-compose.client.yml --env-file deploy/clients/acme/.env"
$COMPOSE run --rm ingest python scripts/ingest.py --dry-run \
  --qa-json evaluation/baselines/chunk-qa.json
```

No database, no API keys, nothing stored, no embedding bill. It prints a quality report and writes
every chunk to `chunked/` for inspection.

**Read it properly** (`chunking-guide.md` has the full guide). `oversized` and `empty` are defects
and must be zero. `mid_sentence_start`, `duplicate` and `mostly_url` usually point at the source
documents, not at chunking, and are what `extraction.pdf.strip_patterns` and `skip_sections` are
for. Iterate here — it is free — and go back to step 3 until the report is clean.

Then **open two or three files in `chunked/`** and read them. The report cannot tell you that a
chunk is useless, only that it is the right size.

## 6. Ingest

```bash
$COMPOSE up -d                  # migrations run to completion, then the API starts
$COMPOSE run --rm ingest
```

The run prints what it added, updated, skipped and removed. A source it could not read is reported
as `failed`, by name — check for those. Re-running is how you re-sync, and it is cheap: unchanged
files are skipped on their hash without being opened.

## 7. Write their evaluation set and record a baseline

This is the step it is tempting to skip, and it is the one that makes every later change safe.

Replace `evaluation/questions.yaml` with **questions about their documents** — 20–30 real ones,
ideally from whoever asked for the system. Include a misspelling and an out-of-scope question.

```bash
$COMPOSE run --rm ingest python scripts/evaluate.py --check-dataset   # no database needed
$COMPOSE run --rm ingest python scripts/evaluate.py \
  --save-baseline evaluation/baselines/retrieval.json
```

`--check-dataset` separates a typo in your questions from a real retrieval failure. Then record
both baselines (chunk quality from step 5, retrieval here) **with this client's deployment**, so
that from now on a change to chunking or retrieval that lowers recall@k or MRR fails the check
rather than being an opinion.

If recall is poor, tune `retrieval` and `chunking` here — against the numbers, not against a
feeling — and re-run. Keep a change only if it measurably helped.

## 8. Issue the key

```bash
$COMPOSE run --rm ingest python scripts/create_api_key.py --label "acme-frontend"
```

Printed **once**; only its hash is stored. Issue one per consumer, not one shared between them —
they are revoked independently, and a shared key cannot be rotated without breaking everything at
once.

## 9. Check it before handing it over

```bash
curl -X POST http://localhost:8000/api/query \
  -H "X-API-Key: <the-key>" -H "Content-Type: application/json" \
  -d '{"question": "<something from their corpus>"}'
```

Confirm all five:

- a real question returns a grounded answer **with citations to their documents**
- an out-of-scope question returns 200 saying it is not in the knowledge base, with `sources: []`
- a missing or wrong key returns 401
- exceeding `api.rate_limit` returns 429 with `Retry-After`
- the logs hold no key and no question (`logging.format: json` and read a few lines)

## 10. Hand over

The consumer needs three things: **the URL**, **their key**, and the request/response shape from
`architecture-context.md` §5. Not the config, not the database, not the admin token.

Tell them plainly:

- the key goes in their **server-side** environment, never in a browser. A browser holding it can
  spend their LLM budget. That is also why CORS is off by default — see `deploy/README.md`.
- an empty `sources` list with a 200 is a normal answer, not an error
- 429 means slow down, and `Retry-After` says how long
- if the key leaks, tell us: revocation takes effect on the next request

## Before it faces the internet

The checklist is in `deploy/README.md` — TLS in front, no published database port, `/docs`
protected or disabled, `/internal` off the public route, JSON logs, and a database backup. The
corpus can be re-ingested at the cost of another embedding bill; the API keys cannot be recovered
at all.

## Keeping it running

| When | Do |
|---|---|
| their documents change | re-run ingestion; unchanged files are not opened |
| you improve a reader | bump its `version`, then re-ingest — otherwise the improvement reaches new documents only |
| you change chunking or retrieval | record baselines first, keep the change only if the numbers improved |
| a consumer is retired | revoke its key; the row stays, so past use is still accountable |
| you fix a bug for one client | it is in the shared codebase, so rebuild and redeploy the others. That is the whole point of not forking |
