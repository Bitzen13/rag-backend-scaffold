from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from embedding.base import EmbeddingError
from embedding.factory import build_embedding_provider
from embedding.openai_provider import BATCH_SIZE, OpenAIEmbeddingProvider
from settings import load_settings


class FakeEmbeddings:
    def __init__(
        self,
        *,
        shuffle: bool = False,
        drop_last: bool = False,
        fail: bool = False,
        timeout: bool = False,
    ):
        self.calls: list[list[str]] = []
        self._shuffle = shuffle
        self._drop_last = drop_last
        self._fail = fail
        self._timeout = timeout

    async def create(self, *, model: str, input: list[str]) -> Any:
        self.calls.append(input)
        if self._timeout:
            # What the SDK raises once its own bounded retries are exhausted.
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
        if self._fail:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
        data = [SimpleNamespace(index=i, embedding=[float(len(t))]) for i, t in enumerate(input)]
        if self._drop_last:
            data = data[:-1]
        if self._shuffle:
            data.reverse()
        return SimpleNamespace(data=data)


def provider(embeddings: FakeEmbeddings) -> OpenAIEmbeddingProvider:
    client = cast(AsyncOpenAI, SimpleNamespace(embeddings=embeddings))
    return OpenAIEmbeddingProvider("sk-test", "text-embedding-3-small", client=client)


async def test_returns_vectors_in_input_order() -> None:
    fake = FakeEmbeddings(shuffle=True)
    assert await provider(fake).embed(["a", "bbb", "cc"]) == [[1.0], [3.0], [2.0]]


async def test_batches_large_inputs() -> None:
    fake = FakeEmbeddings()
    texts = ["x"] * (BATCH_SIZE + 5)
    vectors = await provider(fake).embed(texts)
    assert len(vectors) == len(texts)
    assert [len(c) for c in fake.calls] == [BATCH_SIZE, 5]


async def test_count_mismatch_raises() -> None:
    with pytest.raises(EmbeddingError):
        await provider(FakeEmbeddings(drop_last=True)).embed(["a", "b"])


async def test_provider_errors_are_wrapped() -> None:
    with pytest.raises(EmbeddingError):
        await provider(FakeEmbeddings(fail=True)).embed(["a"])


def test_model_name() -> None:
    assert provider(FakeEmbeddings()).model_name == "text-embedding-3-small"


# --- bounds: timeouts and retries come from config -----------------------------------------------


def test_the_configured_timeout_and_retry_count_reach_the_sdk_client() -> None:
    """The SDK does the retrying; what matters is that it is bounded by config, not a constant."""
    built = OpenAIEmbeddingProvider(
        "sk-test", "text-embedding-3-small", timeout_seconds=7.5, max_retries=1
    )

    assert built._client.timeout == 7.5
    assert built._client.max_retries == 1


def test_the_factory_passes_the_configured_bounds_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings(env_file=None)
    bounded = settings.model_copy(
        update={
            "models": settings.models.model_copy(
                update={"embedding_timeout_seconds": 3.0, "embedding_max_retries": 0}
            )
        }
    )

    built = cast(OpenAIEmbeddingProvider, build_embedding_provider(bounded))

    assert built._client.timeout == 3.0
    assert built._client.max_retries == 0


async def test_a_timeout_that_outlives_the_retries_is_an_embedding_error() -> None:
    """It must surface as an upstream failure (a 503), not as an empty result."""
    with pytest.raises(EmbeddingError, match="failed"):
        await provider(FakeEmbeddings(timeout=True)).embed(["a"])
