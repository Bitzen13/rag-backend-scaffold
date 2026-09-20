from fastapi import FastAPI

from api import health
from settings import get_settings


def create_app() -> FastAPI:
    get_settings()  # fail fast on invalid configuration
    app = FastAPI(title="RAG Backend")
    app.include_router(health.router)
    return app


app = create_app()
