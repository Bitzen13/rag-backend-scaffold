"""POST /api/query: auth, validation, the error shape, the empty-retrieval path, and the docs.

The database and the providers are replaced through `Depends` overrides, so these tests need no
Postgres and call no real LLM or embedding API. The app's lifespan is deliberately not started
(`TestClient` is not used as a context manager), because every dependency that would need those
start-up resources is overridden.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from api.dependencies import get_api_key_store, get_query_service
from api.errors import UPSTREAM_MESSAGE
from db.models import ApiKey
from embedding.base import EmbeddingError
from generation.base import LLMError
from main import create_app
from retrieval.retriever import RetrievedChunk, Retriever
from services.api_keys import ApiKeyStore
from services.query_service import QueryService
from settings import get_settings
from tests.test_api_keys import FakeApiKeyStore, add_key
from vector_store.base import VectorFilter

HEADER = get_settings().auth.api_key_header
ANSWER = "Rayleigh scattering."


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.questions: list[str] = []
        self.filters: VectorFilter | None = None

    async def retrieve(
        self, question: str, filters: VectorFilter | None = None
    ) -> list[RetrievedChunk]:
        self.questions.append(question)
        self.filters = filters
        if self.error is not None:
            raise self.error
        return self.chunks


class FakeLLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def generate(self, *, system: str, user: str) -> str:
        if self.error is not None:
            raise self.error
        return ANSWER


class BrokenApiKeyStore:
    """The database is unreachable while the key is being checked."""

    async def create(self, *, key_hash: str, label: str) -> ApiKey:
        raise SQLAlchemyError("connection refused")

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


@dataclass
class Harness:
    client: TestClient
    key: str  # an active key
    revoked_key: str  # a key that was issued and then revoked
    retriever: FakeRetriever

    def post(self, body: Any, key: str | None = None) -> httpx.Response:
        """POST the body, authenticated with `key` (the active one by default; None sends none)."""
        headers = {} if key == "" else {HEADER: key if key is not None else self.key}
        return cast(httpx.Response, self.client.post("/api/query", json=body, headers=headers))


NO_KEY = ""  # `post(..., key=NO_KEY)` sends no header at all


@contextmanager
def harness(
    chunks: list[RetrievedChunk] | None = None,
    *,
    retrieval_error: Exception | None = None,
    llm_error: Exception | None = None,
    store: ApiKeyStore | None = None,
) -> Iterator[Harness]:
    keys = FakeApiKeyStore()
    active = add_key(keys, label="test-consumer")
    revoked = add_key(keys, label="old-frontend", active=False)
    retriever = FakeRetriever(chunks if chunks is not None else [], retrieval_error)
    service = QueryService(
        retriever=cast(Retriever, retriever), llm=FakeLLM(llm_error), excerpt_max_chars=20
    )

    app = create_app()
    app.dependency_overrides[get_api_key_store] = lambda: store if store is not None else keys
    app.dependency_overrides[get_query_service] = lambda: service
    try:
        yield Harness(TestClient(app), active, revoked, retriever)
    finally:
        app.dependency_overrides.clear()


def one_chunk() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            uuid.uuid4(), "sky.md", "The sky is blue because of Rayleigh scattering.", 0.9
        )
    ]


@pytest.fixture
def api() -> Iterator[Harness]:
    with harness(one_chunk()) as ready:
        yield ready


@pytest.fixture
def empty_api() -> Iterator[Harness]:
    """Nothing reached the similarity threshold."""
    with harness([]) as ready:
        yield ready


# --- the happy path ----------------------------------------------------------------------------


def test_answer_and_sources_are_returned(api: Harness) -> None:
    response = api.post({"question": "Why is the sky blue?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == ANSWER
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert uuid.UUID(source["chunk_id"])
    assert source["source_uri"] == "sky.md"
    assert source["excerpt"] == "The sky is blue bec…"
    assert api.retriever.questions == ["Why is the sky blue?"]


def test_question_is_trimmed_before_retrieval(api: Harness) -> None:
    assert api.post({"question": "  Why is the sky blue?  "}).status_code == 200
    assert api.retriever.questions == ["Why is the sky blue?"]


def test_filters_reach_the_retriever(api: Harness) -> None:
    response = api.post({"question": "Why?", "filters": {"source_type": "file"}})

    assert response.status_code == 200
    assert api.retriever.filters == VectorFilter(source_type="file")


def test_no_filters_means_no_filtering(api: Harness) -> None:
    api.post({"question": "Why?"})
    assert api.retriever.filters is None


# --- empty retrieval is a normal answer ---------------------------------------------------------


def test_empty_retrieval_is_a_200_with_no_sources(empty_api: Harness) -> None:
    response = empty_api.post({"question": "Who won the 1998 World Cup?"})

    assert response.status_code == 200
    assert response.json() == {"answer": ANSWER, "sources": []}


# --- authentication ----------------------------------------------------------------------------


def test_missing_key_is_401(api: Harness) -> None:
    response = api.post({"question": "Why?"}, key=NO_KEY)

    assert response.status_code == 401
    assert response.json() == {
        "error": f"the {HEADER} header is required",
        "code": "missing_api_key",
    }


def test_blank_key_is_401(api: Harness) -> None:
    response = api.post({"question": "Why?"}, key="   ")

    assert response.status_code == 401
    assert response.json()["code"] == "missing_api_key"


def test_unknown_key_is_401(api: Harness) -> None:
    response = api.post({"question": "Why?"}, key="rag_not-a-real-key")

    assert response.status_code == 401
    assert response.json() == {
        "error": "API key is not valid or has been revoked",
        "code": "invalid_api_key",
    }


def test_revoked_key_is_401_and_reads_exactly_like_an_unknown_one(api: Harness) -> None:
    revoked = api.post({"question": "Why?"}, key=api.revoked_key)
    unknown = api.post({"question": "Why?"}, key="rag_not-a-real-key")

    assert revoked.status_code == 401
    assert revoked.json() == unknown.json()


def test_auth_is_checked_before_the_body_is_validated(api: Harness) -> None:
    """A caller with no key learns nothing about what the payload should look like."""
    response = api.post({"not-a-question": True}, key=NO_KEY)

    assert response.status_code == 401
    assert response.json()["code"] == "missing_api_key"


def test_retrieval_never_runs_without_a_valid_key(api: Harness) -> None:
    api.post({"question": "Why?"}, key=NO_KEY)
    api.post({"question": "Why?"}, key="rag_wrong")
    api.post({"question": "Why?"}, key=api.revoked_key)

    assert api.retriever.questions == []


# --- request validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},  # question missing
        {"question": ""},  # empty
        {"question": "   "},  # whitespace only
        {"question": 17},  # wrong type
        {"question": None},
        {"question": "Why?", "extra": "field"},  # unknown field
        {"question": "Why?", "filters": {"unknown": "x"}},  # unknown filter
        {"question": "Why?", "filters": "file"},  # filters not an object
        [{"question": "Why?"}],  # not an object at all
    ],
)
def test_malformed_request_is_400_with_the_standard_shape(api: Harness, body: Any) -> None:
    response = api.post(body)

    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == "invalid_request"
    assert payload["error"]  # says which field, and what is wrong with it
    assert api.retriever.questions == []


def test_question_over_the_configured_limit_is_400(api: Harness) -> None:
    limit = get_settings().api.max_question_chars

    assert api.post({"question": "a" * limit}).status_code == 200
    response = api.post({"question": "a" * (limit + 1)})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert str(limit) in response.json()["error"]
    assert len(api.retriever.questions) == 1  # the oversized question never reached retrieval


def test_body_must_be_json(api: Harness) -> None:
    response = api.client.post("/api/query", content="not json", headers={HEADER: api.key})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


# --- upstream failures -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        {"retrieval_error": EmbeddingError("openai is down")},
        {"llm_error": LLMError("anthropic is down")},
        {"retrieval_error": SQLAlchemyError("connection refused")},
    ],
)
def test_upstream_failure_is_503_without_leaking_the_detail(failure: dict[str, Exception]) -> None:
    with harness([], **failure) as api:  # type: ignore[arg-type]
        response = api.post({"question": "Why?"})

    assert response.status_code == 503
    assert response.json() == {"error": UPSTREAM_MESSAGE, "code": "upstream_unavailable"}
    assert "down" not in response.text
    assert "refused" not in response.text


def test_database_failure_during_auth_is_503_not_401() -> None:
    with harness([], store=BrokenApiKeyStore()) as api:
        response = api.post({"question": "Why?"}, key="rag_anything")

    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"


# --- other routes keep the same shape ----------------------------------------------------------


def test_unknown_route_uses_the_same_error_shape(api: Harness) -> None:
    response = api.client.get("/api/nope")

    assert response.status_code == 404
    assert response.json() == {"error": "Not Found", "code": "not_found"}


def test_wrong_method_uses_the_same_error_shape(api: Harness) -> None:
    response = api.client.get("/api/query")

    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


# --- OpenAPI -----------------------------------------------------------------------------------


def test_openapi_documents_the_route_and_its_errors(api: Harness) -> None:
    schema = api.client.get("/openapi.json").json()
    operation = schema["paths"]["/api/query"]["post"]

    assert operation["summary"] == "Ask a question, answered from the ingested documents"
    assert set(operation["responses"]) == {"200", "400", "401", "503"}
    for status in ("400", "401", "503"):
        ref = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
    assert set(schema["components"]["schemas"]["ErrorResponse"]["properties"]) == {"error", "code"}
    assert set(schema["components"]["schemas"]["QueryResponse"]["properties"]) == {
        "answer",
        "sources",
    }


def test_openapi_does_not_advertise_the_422_this_api_never_returns(api: Harness) -> None:
    schema = api.client.get("/openapi.json").json()

    assert all(
        "422" not in operation.get("responses", {})
        for path in schema["paths"].values()
        for operation in path.values()
    )
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_openapi_documents_the_configured_api_key_header(api: Harness) -> None:
    schema = api.client.get("/openapi.json").json()
    scheme_name, scheme = next(iter(schema["components"]["securitySchemes"].items()))

    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == HEADER
    assert schema["paths"]["/api/query"]["post"]["security"] == [{scheme_name: []}]


def test_health_needs_no_key(api: Harness) -> None:
    assert api.client.get("/health").status_code == 200
    schema = api.client.get("/openapi.json").json()
    assert "security" not in schema["paths"]["/health"]["get"]
