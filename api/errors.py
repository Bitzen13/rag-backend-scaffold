"""The one error shape the API returns: `{"error": ..., "code": ...}`.

Every failure — raised here, raised by FastAPI's validation, or raised by an upstream provider —
is funnelled through these handlers, so a consumer can branch on `code` and never has to parse a
message. `install_error_handlers` registers them; `document_validation_errors_as_400` keeps the
OpenAPI document honest about the status code that actually comes back.
"""

import logging
from collections.abc import Mapping
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from embedding.base import EmbeddingError
from generation.base import LLMError

logger = logging.getLogger(__name__)

# Deliberately generic: which provider failed, and why, goes to the log and not to the caller.
UPSTREAM_MESSAGE = "an upstream service (LLM, embedding, or database) is unavailable"


class ErrorCode(StrEnum):
    """Stable identifiers. Renaming one is a breaking change to the consumer contract."""

    MISSING_API_KEY = "missing_api_key"
    INVALID_API_KEY = "invalid_api_key"
    INVALID_REQUEST = "invalid_request"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


class ErrorResponse(BaseModel):
    """The body of every error response, whatever the status code."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "API key is not valid or has been revoked",
                "code": "invalid_api_key",
            }
        }
    )

    error: str  # human-readable, safe to show a developer
    code: str  # machine-readable; one of ErrorCode, or the status phrase for a generic HTTP error


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": "Malformed body, or a question that is empty or too long",
    },
    401: {"model": ErrorResponse, "description": "The API key is missing, unknown, or revoked"},
    503: {"model": ErrorResponse, "description": "An upstream service is unreachable"},
}


class ApiError(Exception):
    """Raised anywhere in a request to return the standard error body."""

    def __init__(self, status_code: int, code: ErrorCode, error: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.code = code
        self.error = error


def error_response(status_code: int, code: str, error: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, "code": code})


def _field_path(location: tuple[int | str, ...]) -> str:
    """Name the field the caller sent: ("body", "filters", "x") -> "filters.x"."""
    parts = [str(part) for part in location]
    return ".".join(parts[1:]) if parts[:1] == ["body"] and len(parts) > 1 else ".".join(parts)


def install_error_handlers(app: FastAPI) -> None:
    async def on_api_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return error_response(exc.status_code, exc.code.value, exc.error)

    async def on_validation_error(_: Request, exc: Exception) -> JSONResponse:
        """FastAPI's own 422 becomes this API's 400 (code-standards.md)."""
        assert isinstance(exc, RequestValidationError)
        first = exc.errors()[0] if exc.errors() else {}
        path = _field_path(tuple(first.get("loc", ())))
        message = str(first.get("msg", "the request body is malformed"))
        return error_response(
            400, ErrorCode.INVALID_REQUEST.value, f"{path}: {message}" if path else message
        )

    async def on_upstream_error(_: Request, exc: Exception) -> JSONResponse:
        """A provider or the database is unreachable: 503, with the detail logged, not returned."""
        logger.error("upstream failure: %s", exc, exc_info=True)
        return error_response(503, ErrorCode.UPSTREAM_UNAVAILABLE.value, UPSTREAM_MESSAGE)

    async def on_http_exception(_: Request, exc: Exception) -> JSONResponse:
        """404, 405 and friends, in the same shape as everything else."""
        assert isinstance(exc, StarletteHTTPException)
        phrase = HTTPStatus(exc.status_code).phrase
        return error_response(
            exc.status_code, phrase.lower().replace(" ", "_"), str(exc.detail or phrase)
        )

    app.add_exception_handler(ApiError, on_api_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
    for upstream in (EmbeddingError, LLMError, SQLAlchemyError):
        app.add_exception_handler(upstream, on_upstream_error)


def document_validation_errors_as_400(app: FastAPI) -> None:
    """Drop FastAPI's automatic 422 from the schema: this app answers 400 instead.

    Without this the OpenAPI document advertises a status code the API never returns, and a
    generated client would carry a branch that can never run.
    """

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
            paths: Mapping[str, Any] = schema.get("paths", {})
            for operations in paths.values():
                for operation in operations.values():
                    operation.get("responses", {}).pop("422", None)
            schemas = schema.get("components", {}).get("schemas", {})
            for unused in ("HTTPValidationError", "ValidationError"):
                schemas.pop(unused, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
