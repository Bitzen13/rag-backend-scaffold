from types import SimpleNamespace
from typing import Any, cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from embedding.base import EmbeddingError
from embedding.openai_provider import BATCH_SIZE, OpenAIEmbeddingProvider


class FakeEmbeddings:
    def __init__(self, *, shuffle: bool = False, drop_last: bool = False, fail: bool = False):
        self.calls: list[list[str]] = []
        self._shuffle = shuffle
        self._drop_last = drop_last
        self._fail = fail

    async def create(self, *, model: str, input: list[str]) -> Any:
        self.calls.append(input)
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
