"""Typed settings: tunables from config.yaml, secrets from environment variables."""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config.yaml")
CONFIG_PATH_ENV_VAR = "CONFIG_PATH"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ChunkingStrategy = Literal["fixed_size", "document_aware"]


class ChunkingConfig(_Section):
    chunk_size: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    # Chunking strategy per document, keyed by source_uri (path relative to extraction.path).
    # Chosen manually: a document with no entry is not ingested.
    documents: dict[str, ChunkingStrategy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _overlap_smaller_than_size(self) -> Self:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class PdfConfig(_Section):
    """Cleanup rules for text extracted from PDFs (regular expressions; see extraction/pdf)."""

    strip_patterns: tuple[str, ...] = ()  # removed wherever they appear
    truncate_patterns: tuple[str, ...] = ()  # text from the first match onward is dropped
    skip_sections: tuple[str, ...] = ()  # headings whose whole section is left out

    @model_validator(mode="after")
    def _patterns_compile(self) -> Self:
        for pattern in (*self.strip_patterns, *self.truncate_patterns):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return self


class ExtractionConfig(_Section):
    provider: Literal["filesystem"]
    path: Path
    extensions: tuple[str, ...] = Field(default=(".md", ".txt"), min_length=1)
    pdf: PdfConfig = Field(default_factory=PdfConfig)

    @model_validator(mode="after")
    def _extensions_have_leading_dot(self) -> Self:
        for extension in self.extensions:
            if not extension.startswith(".") or len(extension) < 2:
                raise ValueError(f"extension {extension!r} must look like '.md'")
        return self


class RetrievalConfig(_Section):
    top_k: int = Field(ge=1)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    excerpt_max_chars: int = Field(default=300, ge=1)
    # Typo-tolerant keyword search alongside the vector search (0 disables it).
    fuzzy_top_k: int = Field(default=5, ge=0)
    fuzzy_min_similarity: float = Field(default=0.6, ge=0.0, le=1.0)
    fuzzy_max_matches_per_phrase: int = Field(default=50, ge=1)
    fuzzy_min_relative_evidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ModelsConfig(_Section):
    embedding_provider: Literal["openai", "sentence_transformers"]
    embedding_model: str = Field(min_length=1)
    llm_provider: Literal["anthropic", "openai"]
    llm_model: str = Field(min_length=1)
    llm_max_tokens: int = Field(default=1024, ge=1)


class VectorDbConfig(_Section):
    provider: Literal["pgvector", "pinecone", "qdrant", "weaviate"]


class AuthConfig(_Section):
    api_key_header: str = Field(min_length=1)


class Secrets(BaseSettings):
    """Secrets read from environment variables (or a local .env file). Never from config.yaml."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    database_url: SecretStr | None = None
    vector_db_url: SecretStr | None = None


class Settings(_Section):
    extraction: ExtractionConfig
    chunking: ChunkingConfig
    retrieval: RetrievalConfig
    models: ModelsConfig
    vector_db: VectorDbConfig
    auth: AuthConfig
    secrets: Secrets


def load_settings(config_path: Path | None = None, *, env_file: str | None = ".env") -> Settings:
    """Load and validate settings.

    Raises FileNotFoundError if the config file is missing, ValueError if it is not a YAML
    mapping, and pydantic.ValidationError if any value is invalid.
    """
    path = config_path or Path(os.environ.get(CONFIG_PATH_ENV_VAR, DEFAULT_CONFIG_PATH))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return Settings.model_validate({**raw, "secrets": Secrets(_env_file=env_file)})


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor, suitable as a FastAPI dependency."""
    return load_settings()
