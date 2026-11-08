"""LLM provider contract. Implementations are selected from `models.llm_provider` in config.yaml."""

from typing import Protocol


class LLMError(RuntimeError):
    """The provider failed or returned no usable text."""


class LLMProvider(Protocol):
    async def generate(self, *, system: str, user: str) -> str:
        """Return the model's text reply to a single user message."""
        ...
