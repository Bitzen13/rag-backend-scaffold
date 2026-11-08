import anthropic
from anthropic import AsyncAnthropic

from app.generation.base import LLMError

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3


class AnthropicProvider:
    """Anthropic behind the LLMProvider contract.

    The timeout and retry count come from `models.*` in config.yaml. The SDK retries transient
    failures itself, up to `max_retries`; anything that survives that is raised as an `LLMError`,
    which the API turns into a 503. A timeout arrives here as an `AnthropicError` too, so a slow
    provider and a broken one are reported the same way and neither hangs a request.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        client: AsyncAnthropic | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
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
