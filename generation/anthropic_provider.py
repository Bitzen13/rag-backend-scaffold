import anthropic
from anthropic import AsyncAnthropic

from generation.base import LLMError

TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES
        )

    async def generate(self, *, system: str, user: str) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise LLMError("Anthropic returned an empty response")
        return text
