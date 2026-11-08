from app.embedding.base import EmbeddingProvider
from app.embedding.openai_provider import OpenAIEmbeddingProvider
from app.settings import Settings


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    models = settings.models
    if models.embedding_provider == "openai":
        api_key = settings.secrets.openai_api_key
        if api_key is None or not api_key.get_secret_value():
            raise RuntimeError("OPENAI_API_KEY is not set")
        return OpenAIEmbeddingProvider(
            api_key.get_secret_value(),
            models.embedding_model,
            timeout_seconds=models.embedding_timeout_seconds,
            max_retries=models.embedding_max_retries,
        )
    raise ValueError(f"unsupported embedding provider: {models.embedding_provider}")
