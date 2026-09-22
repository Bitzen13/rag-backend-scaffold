from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import health, query
from api.dependencies import build_resources
from api.errors import document_validation_errors_as_400, install_error_handlers
from settings import get_settings

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


def create_app() -> FastAPI:
    get_settings()  # fail fast on invalid configuration
    app = FastAPI(
        title="RAG Backend",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(query.router)
    install_error_handlers(app)
    document_validation_errors_as_400(app)
    return app


app = create_app()
