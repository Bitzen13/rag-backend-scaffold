# Production image. One image serves every client: the client's own config.yaml, secrets and
# documents are mounted or injected at run time, never baked in (see deploy/README.md).
#
# Two stages so the build tools that compile wheels do not ship to production. The runtime stage
# installs nothing from the network: it copies the virtualenv the builder produced.

# --- builder --------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Only what building wheels needs. None of this reaches the runtime stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime --------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Where config.yaml is read from; mount the client's own file over it.
    CONFIG_PATH=/app/config.yaml

# Optional, for OCR of scanned PDF pages (extraction.pdf.ocr.enabled). Uncomment together with
# the pytesseract line in requirements.txt; both are left out by default because they are large
# and most corpora do not need them.
# RUN apt-get update \
#     && apt-get install --no-install-recommends -y tesseract-ocr \
#     && rm -rf /var/lib/apt/lists/*

# Runs as a non-root user that owns nothing it does not need to. The application writes no files
# in production — chunked/ is a dry-run artefact — so /app is owned by root and merely readable.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rag

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root \
    main.py settings.py observability.py alembic.ini config.yaml ./
COPY --chown=root:root api ./api
COPY --chown=root:root chunking ./chunking
COPY --chown=root:root customisations ./customisations
COPY --chown=root:root db ./db
COPY --chown=root:root embedding ./embedding
COPY --chown=root:root evaluation ./evaluation
COPY --chown=root:root extraction ./extraction
COPY --chown=root:root generation ./generation
COPY --chown=root:root ingestion ./ingestion
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root retrieval ./retrieval
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root services ./services
COPY --chown=root:root vector_store ./vector_store
COPY --chown=root:root docker/gunicorn.conf.py ./docker/gunicorn.conf.py

USER rag

EXPOSE 8000

# /health needs no API key, so the check does not require a credential in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Gunicorn supervises Uvicorn workers: it restarts a worker that dies and reaps one that hangs,
# which a bare `uvicorn` process does not. Migrations are deliberately NOT run here — see
# deploy/README.md: a container that migrates on start would have every worker race the others,
# and a schema change would be applied by whichever replica happened to boot first.
CMD ["gunicorn", "-c", "docker/gunicorn.conf.py", "main:app"]
