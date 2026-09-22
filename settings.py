"""Typed settings: tunables from config.yaml, secrets from environment variables."""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chunking.prose import ProseSplitting
from chunking.tokenizer import TokenizerKind, default_max_chunk_tokens, embedding_input_limit

DEFAULT_CONFIG_PATH = Path("config.yaml")
CONFIG_PATH_ENV_VAR = "CONFIG_PATH"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ChunkingStrategy = Literal["fixed_size", "document_aware"]


class DocumentChunking(_Section):
    """One document's chunking settings. Anything left unset falls back to the global value.

    In config.yaml a document may be written either as a bare strategy name or as this mapping:

        documents:
          notes.md: fixed_size
          report.pdf:
            strategy: document_aware
            chunk_size: 400
            prose_splitting: sentence
    """

    strategy: ChunkingStrategy
    chunk_size: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    prose_splitting: ProseSplitting | None = None

    @model_validator(mode="after")
    def _prose_splitting_applies_to_the_strategy(self) -> Self:
        if self.prose_splitting is not None and self.strategy != "document_aware":
            raise ValueError(
                f"prose_splitting has no effect on strategy {self.strategy!r}: "
                "it is a setting of document_aware"
            )
        return self


class ChunkingConfig(_Section):
    # Sizes are in tokens of the configured embedding model (see chunking.tokenizer).
    chunk_size: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    # Hard cap per chunk, so no chunk can be silently truncated by the embedding model. Left
    # unset it is derived from that model's input limit, which fails loudly for a model whose
    # limit is not known here.
    max_chunk_tokens: int | None = Field(default=None, gt=0)
    tokenizer: TokenizerKind = "auto"
    prose_splitting: ProseSplitting = "paragraph"
    # Chunking settings per document, keyed by source_uri (path relative to extraction.path).
    # Chosen manually: a document with no entry is not ingested.
    documents: dict[str, DocumentChunking] = Field(default_factory=dict)

    @field_validator("documents", mode="before")
    @classmethod
    def _accept_bare_strategy_names(cls, value: object) -> object:
        """Allow "notes.md: fixed_size" as shorthand for "notes.md: {strategy: fixed_size}"."""
        if not isinstance(value, dict):
            return value
        return {
            key: {"strategy": entry} if isinstance(entry, str) else entry
            for key, entry in value.items()
        }

    @model_validator(mode="after")
    def _overlap_smaller_than_size(self) -> Self:
        for label, size, overlap in self.size_pairs():
            if overlap >= size:
                raise ValueError(f"{label}: chunk_overlap must be smaller than chunk_size")
        return self

    def size_pairs(self) -> list[tuple[str, int, int]]:
        """Every effective (label, chunk_size, chunk_overlap) this config produces."""
        pairs = [("chunking", self.chunk_size, self.chunk_overlap)]
        for uri, entry in self.documents.items():
            pairs.append(
                (
                    f"chunking.documents.{uri}",
                    self.chunk_size if entry.chunk_size is None else entry.chunk_size,
                    self.chunk_overlap if entry.chunk_overlap is None else entry.chunk_overlap,
                )
            )
        return pairs


class OcrConfig(_Section):
    """Optional OCR for PDF pages that hold an image of text rather than text.

    Off by default: it needs Tesseract installed (the pytesseract package plus the tesseract
    binary) and it is slow. When enabled but unavailable, extraction fails with a message naming
    what is missing rather than quietly ingesting empty pages.
    """

    enabled: bool = False
    language: str = Field(default="eng", min_length=1)  # a Tesseract language code
    min_characters: int = Field(default=20, ge=0)  # fewer characters: the page looks scanned
    resolution: int = Field(default=300, ge=72, le=1200)


class PdfConfig(_Section):
    """Cleanup rules for text extracted from PDFs (regular expressions; see extraction/pdf)."""

    strip_patterns: tuple[str, ...] = ()  # removed wherever they appear
    truncate_patterns: tuple[str, ...] = ()  # text from the first match onward is dropped
    skip_sections: tuple[str, ...] = ()  # headings whose whole section is left out
    borderless_tables: bool = True  # also detect tables that are aligned but not ruled
    ocr: OcrConfig = Field(default_factory=OcrConfig)

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
    # Which files are picked up. The reader for each one is chosen by extension (see
    # extraction/readers/registry.py); an extension with no reader is read as plain text.
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


class ApiConfig(_Section):
    """Limits enforced at the public API boundary, before any embedding or LLM call is made."""

    max_question_chars: int = Field(default=2000, ge=1)


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
    api: ApiConfig = Field(default_factory=ApiConfig)
    secrets: Secrets

    @property
    def max_chunk_tokens(self) -> int:
        """The hard cap per chunk: from config, or derived from the embedding model's limit."""
        cap = self.chunking.max_chunk_tokens or default_max_chunk_tokens(
            self.models.embedding_model
        )
        if cap is None:  # unreachable once loaded; kept so this cannot return None
            raise ValueError("chunking.max_chunk_tokens is required for this embedding model")
        return cap

    @model_validator(mode="after")
    def _chunks_fit_the_embedding_model(self) -> Self:
        model = self.models.embedding_model
        limit = embedding_input_limit(model)
        cap = self.chunking.max_chunk_tokens
        if cap is None:
            cap = default_max_chunk_tokens(model)
            if cap is None:
                raise ValueError(
                    f"the input limit of embedding model {model!r} is not known here: set "
                    "chunking.max_chunk_tokens in config.yaml to that model's limit, with a "
                    "little room to spare"
                )
        elif limit is not None and cap > limit:
            raise ValueError(
                f"chunking.max_chunk_tokens ({cap}) exceeds the {limit}-token input limit of "
                f"embedding model {model!r}, so chunks would be truncated"
            )
        for label, size, _ in self.chunking.size_pairs():
            if size > cap:
                raise ValueError(
                    f"{label}: chunk_size ({size}) exceeds the hard cap of {cap} tokens"
                )
        return self


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
