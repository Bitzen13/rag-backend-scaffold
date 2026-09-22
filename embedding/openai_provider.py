from collections.abc import Sequence

import openai
from openai import AsyncOpenAI

from embedding.base import EmbeddingError

BATCH_SIZE = 96
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3


class OpenAIEmbeddingProvider:
    """OpenAI embeddings behind the EmbeddingProvider contract.

    The timeout and retry count come from `models.*` in config.yaml. The SDK's retries are
    bounded by `max_retries`; a failure that outlives them — a timeout included — is raised as an
    `EmbeddingError` and answered 503, rather than retried until the caller gives up.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        client: AsyncOpenAI | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            try:
                response = await self._client.embeddings.create(model=self._model, input=batch)
            except openai.OpenAIError as exc:
                raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc
            items = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in items] != list(range(len(batch))):
                raise EmbeddingError("OpenAI returned a different number of embeddings than sent")
            vectors.extend(list(item.embedding) for item in items)
        return vectors
