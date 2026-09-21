from generation.anthropic_provider import AnthropicProvider
from generation.base import LLMProvider
from settings import Settings


def build_llm_provider(settings: Settings) -> LLMProvider:
    models = settings.models
    if models.llm_provider == "anthropic":
        api_key = settings.secrets.anthropic_api_key
        if api_key is None or not api_key.get_secret_value():
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(
            api_key.get_secret_value(), models.llm_model, models.llm_max_tokens
        )
    raise ValueError(f"unsupported LLM provider: {models.llm_provider}")
