"""POST /api/query — the sole public integration surface.

The request and response shapes here are the contract external consumers are built against
(architecture-context.md §5): a change to them must be backward-compatible or versioned.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.dependencies import AuthorizedDep, QueryLoggerDep, QueryServiceDep, SettingsDep
from api.errors import ERROR_RESPONSES, ApiError, ErrorCode
from vector_store.base import VectorFilter

router = APIRouter(tags=["query"])


class QueryFilters(BaseModel):
    """Optional metadata narrowing. Unset means search everything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str | None = Field(default=None, min_length=1, max_length=64, examples=["file"])


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=1,
        description="The question to answer. Length is capped by `api.max_question_chars`.",
        examples=["What is this about?"],
    )
    filters: QueryFilters | None = None

    @field_validator("question")
    @classmethod
    def _not_only_whitespace(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("must not be empty")
        return question


class QuerySource(BaseModel):
    """One retrieved chunk, traceable back to the document it came from."""

    chunk_id: uuid.UUID
    source_uri: str
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    # Empty when nothing reached the similarity threshold. That is a normal answer, not an error:
    # the LLM is still called, with an empty context, so small talk still works and a factual
    # question is declined rather than fabricated.
    sources: list[QuerySource]


@router.post(
    "/api/query",
    response_model=QueryResponse,
    summary="Ask a question, answered from the ingested documents",
    description=(
        "Embeds the question, retrieves the most relevant chunks by meaning and by spelling, and "
        "asks the LLM to answer from them alone. When nothing relevant is found the reply is a "
        "normal 200 with an empty `sources` list and an answer saying so."
    ),
    responses=ERROR_RESPONSES,
)
async def query(
    payload: QueryRequest,
    settings: SettingsDep,
    service: QueryServiceDep,
    caller: AuthorizedDep,
    query_log: QueryLoggerDep,
) -> QueryResponse:
    limit = settings.api.max_question_chars
    if len(payload.question) > limit:
        raise ApiError(
            400,
            ErrorCode.INVALID_REQUEST,
            f"question: must be at most {limit} characters, got {len(payload.question)}",
        )
    filters = (
        VectorFilter(source_type=payload.filters.source_type)
        if payload.filters is not None
        else None
    )
    result = await service.answer(payload.question, filters)
    # After the answer exists, and never at its expense: see services/usage.py.
    await query_log.record(
        caller.api_key.id, payload.question, result.answer, [s.chunk_id for s in result.sources]
    )
    return QueryResponse(
        answer=result.answer,
        sources=[
            QuerySource(chunk_id=s.chunk_id, source_uri=s.source_uri, excerpt=s.excerpt)
            for s in result.sources
        ],
    )
