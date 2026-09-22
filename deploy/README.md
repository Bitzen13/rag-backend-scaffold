# Deploying — one instance per client

One image. One stack per client company, each with its own database, its own documents, its own
`config.yaml`, its own secrets and its own API keys. Isolation is physical rather than a `WHERE
client_id = ...` somebody has to remember to write — the reasoning, and what would have to be
revisited to change it, is in *Considered and not adopted* in
`context/design-decisions.md`.

Standing a client up is this template plus a directory, and no code change. The runbook with the
order of operations is `context/client-onboarding.md`; this file is the mechanics.

## Layout

```
deploy/
  docker-compose.client.yml     the stack, parameterised by CLIENT
  clients/
    example/                    copy this per client
      config.yaml               this client's tuning (the file that differs)
      .env.example              this client's secrets and ports (copy to .env)
      documents/                this client's corpus, mounted read-only
```

`CLIENT` names the directory and prefixes the Compose project, so two clients on one host get
separate volumes, separate containers and no way to reach each other's data.

## Build the image

```bash
docker build -t rag-backend:1.0.0 .
```

Multi-stage: the compiler that builds wheels stays in the builder stage. The runtime stage copies
the finished virtualenv and installs nothing from the network. It runs as UID 10001, owns none of
the application files, and writes nothing — `chunked/` is a `--dry-run` artefact and is not
produced in production.

Pin the tag in each client's `.env` (`IMAGE=rag-backend:1.0.0`). `latest` means a restart can
quietly change versions.

## Stand up a client

```bash
cp -r deploy/clients/example deploy/clients/acme
cp deploy/clients/acme/.env.example deploy/clients/acme/.env
# fill in .env: provider key, POSTGRES_PASSWORD, API_PORT, optionally ADMIN_API_TOKEN
# edit config.yaml: extraction.extensions, chunking.documents, models, retrieval, api.rate_limit
# put the corpus in deploy/clients/acme/documents/

export CLIENT=acme
docker compose -f deploy/docker-compose.client.yml --env-file deploy/clients/acme/.env up -d
```

The stack runs migrations to completion, then starts the API. Ingestion is a job, not a service:

```bash
docker compose -f deploy/docker-compose.client.yml --env-file deploy/clients/acme/.env \
  run --rm ingest
```

Issue the consumer's key:

```bash
docker compose -f deploy/docker-compose.client.yml --env-file deploy/clients/acme/.env \
  run --rm ingest python scripts/create_api_key.py --label "acme-frontend"
```

## Why migrations are their own step

They are not in the image's `CMD` and not in the worker start-up. A container that migrated on
boot would have every Gunicorn worker race the others, and with more than one replica the schema
would be applied by whichever container happened to start first. The `migrate` service runs to
completion and the API waits on it (`service_completed_successfully`).

Roll forward, never automatically back: `alembic downgrade` on a database holding a client's
corpus drops columns with data in them. Take the backup first.

## Process model

Gunicorn supervises Uvicorn workers (`docker/gunicorn.conf.py`). Gunicorn is there for
supervision — restarting a worker that dies, reaping one that hangs — not for concurrency: the
workers are async, so concurrency comes from the event loop. Two workers is a sensible default
and `WEB_CONCURRENCY` overrides it. Each worker holds its own database pool and its own provider
clients, so more workers is not free.

`preload_app` is deliberately off: the lifespan opens those pools per worker, and preloading would
fork them into every child as shared file descriptors.

## What each client changes, and what it must not

| Changes per client | Stays identical |
|---|---|
| `config.yaml` — corpus, chunking, models, retrieval, limits | the image |
| `.env` — provider keys, database password, port, admin token | the pipeline code |
| `documents/` — their corpus | the migrations |
| their API keys, issued per consumer | the readers and chunking rules, unless a client genuinely needs one of their own |

A client that seems to need a code change needs `customisations/` instead — a module the shared
codebase does not own, reached through the Protocols the pipeline already defines. See
`customisations/README.md`. A fork is the one answer that is wrong: it is how one bug comes to be
found and fixed five times, differently.

## Before it faces the internet

- **Put a TLS-terminating proxy in front.** The container speaks plain HTTP on 8000; an API key in
  a header over plain HTTP is a leaked key.
- **Do not publish the database port.** The template does not.
- **Protect or disable `/docs` and `/redoc`.** They are developer tools.
- **Decide about `/internal`.** Leaving `ADMIN_API_TOKEN` unset means it is not mounted at all.
  If it is set, keep it off the public route in the proxy as well: the token is the second lock.
- **Set `logging.format: json`** so a shipper can index the fields. Secrets are redacted either
  way (`observability.py`).
- **Back up the database.** It holds the chunks, the vectors and the API keys. The corpus can be
  re-ingested, at the cost of another embedding bill; the keys cannot be recovered at all.
