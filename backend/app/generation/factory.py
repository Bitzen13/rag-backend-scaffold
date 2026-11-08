"""Selecting the LLM provider from `models.llm_provider`.

Two providers behind one contract. The secret each one needs is checked here, at build time, so a
deployment configured for a provider whose key is missing fails at start-up with the name of the
variable to set — not on the first question a consumer asks.
"""

from pydantic import SecretStr

from app.generation.anthropic_provider import AnthropicProvider
from app.generation.base import LLMProvider
from app.generation.openai_provider import OpenAIProvider
from app.settings import Settings


def _required(secret: SecretStr | None, variable: str) -> str:
    if secret is None or not secret.get_secret_value():
        raise RuntimeError(f"{variable} is not set")
    return secret.get_secret_value()


def build_llm_provider(settings: Settings) -> LLMProvider:
    models = settings.models
    if models.llm_provider == "anthropic":
        return AnthropicProvider(
            _required(settings.secrets.anthropic_api_key, "ANTHROPIC_API_KEY"),
            models.llm_model,
            models.llm_max_tokens,
            timeout_seconds=models.llm_timeout_seconds,
            max_retries=models.llm_max_retries,
        )
    if models.llm_provider == "openai":
        return OpenAIProvider(
            _required(settings.secrets.openai_api_key, "OPENAI_API_KEY"),
            models.llm_model,
            models.llm_max_tokens,
            timeout_seconds=models.llm_timeout_seconds,
            max_retries=models.llm_max_retries,
        )
    raise ValueError(f"unsupported LLM provider: {models.llm_provider}")
