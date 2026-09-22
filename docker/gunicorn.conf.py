"""Gunicorn configuration for the production image.

Gunicorn supervises Uvicorn workers rather than replacing them: the workers are async (every
route and every provider call is `async def`), and Gunicorn is what restarts one that dies and
reaps one that stops answering. A bare `uvicorn` process does neither.

Every value is overridable by environment variable, because the right worker count depends on the
machine a client is deployed onto and one image serves them all.

Two things this deliberately does not do:

* **No `preload_app`.** The lifespan opens the database engine and the provider clients per
  worker; preloading would fork those connection pools into every child, where they would be
  shared file descriptors and corrupt each other.
* **No migrations.** They run as their own step before a deploy (deploy/README.md). A worker that
  migrated on boot would race the other workers, and with more than one replica a schema change
  would be applied by whichever container happened to start first.
"""

import multiprocessing
import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _default_workers() -> int:
    """`2 * cores + 1` is the usual rule for sync workers and is wrong here.

    These workers are async: a worker spends nearly all of a request awaiting an embedding call,
    an LLM call or Postgres, so concurrency comes from the event loop and not from the worker
    count. Workers exist for CPU parallelism and for isolation from a crash, so one per core is
    the right shape — but each holds its own database pool and its own provider clients, so a
    small container is better served by a couple than by one per core it cannot afford.
    """
    return max(2, min(multiprocessing.cpu_count(), 8))


bind = os.environ.get("BIND", "0.0.0.0:8000")
workers = _int("WEB_CONCURRENCY", _default_workers())
worker_class = "uvicorn.workers.UvicornWorker"

# Longer than the longest upstream bound (models.llm_timeout_seconds, default 60) plus room for
# retries, so Gunicorn kills a worker that has genuinely hung rather than one that is waiting on a
# slow but healthy provider call. A request that outlives its own timeouts is already a 503.
timeout = _int("GUNICORN_TIMEOUT", 180)
graceful_timeout = _int("GUNICORN_GRACEFUL_TIMEOUT", 30)

# Longer than any sane proxy's idle timeout, so the proxy closes idle connections rather than
# Gunicorn closing one the proxy is about to reuse.
keepalive = _int("GUNICORN_KEEPALIVE", 65)

# Recycle workers periodically: cheap insurance against a slow leak in a long-lived process. The
# jitter stops every worker retiring at the same moment and emptying the pool.
max_requests = _int("GUNICORN_MAX_REQUESTS", 2000)
max_requests_jitter = _int("GUNICORN_MAX_REQUESTS_JITTER", 200)

# The application configures its own logging, including the redaction that keeps secrets out of it
# (observability.py). Gunicorn's own lines go to stdout/stderr alongside it.
accesslog = None  # api/middleware.py logs one structured line per request instead
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
