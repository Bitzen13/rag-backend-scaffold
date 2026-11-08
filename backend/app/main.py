import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, health, query
from app.api.dependencies import build_resources
from app.api.errors import document_validation_errors_as_400, install_error_handlers
from app.api.middleware import RequestLogMiddleware
from app.observability import configure_logging
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Retrieval-augmented question answering over an ingested document corpus. "
    "Authenticate with the API key header shown below; keys are issued with "
    "`python scripts/create_api_key.py`."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the database engine and provider clients once, and close them on shutdown."""
    resources = build_resources(get_settings())
    app.state.resources = resources
    try:
        yield
    finally:
        await resources.close()


def install_cors(app: FastAPI, settings: Settings) -> None:
    """Allow browser origins only where a deployment has named them.

    Not installed at all when none are configured, which is the default. A consumer frontend
    calls this API from its own server, where CORS does not apply and where its API key can be
    held safely; a browser calling here directly would have to hold that key itself, which it
    must never do. An origin here is therefore a deliberate exception.
    """
    cors = settings.api.cors
    if not cors.allowed_origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors.allowed_origins),
        allow_credentials=cors.allow_credentials,
        allow_methods=["GET", "POST"],
        allow_headers=[settings.auth.api_key_header, "Content-Type"],
    )
    logger.info("CORS is enabled for %s", ", ".join(cors.allowed_origins))


def create_app() -> FastAPI:
    settings = get_settings()  # fail fast on invalid configuration
    configure_logging(settings)
    app = FastAPI(
        title="RAG Backend",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.add_middleware(RequestLogMiddleware)
    install_cors(app, settings)
    app.include_router(health.router)
    app.include_router(query.router)
    # The internal surface exists only where an operator configured a token for it. Mounting it
    # unconditionally would leave every deployment with an admin path it never asked for.
    if admin.admin_routes_enabled(settings):
        app.include_router(admin.router)
        logger.info("internal admin routes are mounted at /internal")
    install_error_handlers(app)
    document_validation_errors_as_400(app)
    return app


app = create_app()
