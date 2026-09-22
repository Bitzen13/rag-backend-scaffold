"""OpenAI behind the same LLMProvider contract as Anthropic.

The second provider is what makes the interface a real one rather than a shape one implementation
happens to have. It also answers a practical question at onboarding: a client with an OpenAI
account and no Anthropic one is a config change (`models.llm_provider: openai`), not a project.

Everything the API guarantees is preserved here, because it is the contract and not the SDK that
those guarantees belong to: the timeout and retry count come from config, the SDK's retries are
bounded, and a failure that outlives them — or a reply with no text in it — is raised as
`LLMError` and answered 503 rather than returned as an empty answer.
"""

import openai
from openai import AsyncOpenAI

from generation.base import LLMError

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3


class OpenAIProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        client: AsyncOpenAI | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )

    async def generate(self, *, system: str, user: str) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_completion_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc
        if not response.choices:
            raise LLMError("OpenAI returned no choices")
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise LLMError("OpenAI returned an empty response")
        return text
