"""Embedding provider contract. Implementations are selected from `models.embedding_provider`."""

from collections.abc import Sequence
from typing import Protocol


class EmbeddingError(RuntimeError):
    """The provider failed or returned a response that does not match the request."""


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str:
        """Recorded on each chunk (`chunks.embedding_model`) for auditability."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in order."""
        ...
