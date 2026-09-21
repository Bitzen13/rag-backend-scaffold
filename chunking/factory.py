"""Resolving each document's chunker from config: strategy, sizes, splitting mode, tokenizer."""

from chunking.base import Chunker, ChunkingStrategyError, ChunkPlan
from chunking.document_aware import DocumentAwareChunker
from chunking.fixed_size import FixedSizeChunker
from chunking.tokenizer import Tokenizer, build_tokenizer
from settings import ChunkingConfig, ChunkingStrategy, Settings

STRATEGIES: tuple[ChunkingStrategy, ...] = ("fixed_size", "document_aware")


class ConfiguredChunkers:
    """Resolves each document's plan and chunker from `chunking`; there is no default strategy.

    One tokenizer is shared by every chunker, so its encoding is loaded once per run.
    """

    def __init__(
        self, config: ChunkingConfig, *, max_chunk_tokens: int, embedding_model: str
    ) -> None:
        self._config = config
        self._max_chunk_tokens = max_chunk_tokens
        self._tokenizer: Tokenizer = build_tokenizer(config.tokenizer, embedding_model)
        self._chunkers: dict[ChunkPlan, Chunker] = {}

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tokenizer

    def strategy_for(self, source_uri: str) -> ChunkingStrategy:
        return self.plan_for(source_uri).strategy

    def plan_for(self, source_uri: str) -> ChunkPlan:
        entry = self._config.documents.get(source_uri)
        if entry is None:
            raise ChunkingStrategyError(
                f"no chunking strategy set for {source_uri!r}: add it under chunking.documents "
                f"in config.yaml (one of: {', '.join(STRATEGIES)})"
            )
        config = self._config
        return ChunkPlan(
            strategy=entry.strategy,
            chunk_size=config.chunk_size if entry.chunk_size is None else entry.chunk_size,
            chunk_overlap=(
                config.chunk_overlap if entry.chunk_overlap is None else entry.chunk_overlap
            ),
            prose_splitting=(
                config.prose_splitting if entry.prose_splitting is None else entry.prose_splitting
            ),
            max_chunk_tokens=self._max_chunk_tokens,
            tokenizer=self._tokenizer.name,
        )

    def for_document(self, source_uri: str) -> Chunker:
        return self._chunker(self.plan_for(source_uri))

    def _chunker(self, plan: ChunkPlan) -> Chunker:
        chunker = self._chunkers.get(plan)
        if chunker is None:
            chunker = build_chunker(plan, self._tokenizer)
            self._chunkers[plan] = chunker
        return chunker


def build_chunker(plan: ChunkPlan, tokenizer: Tokenizer) -> Chunker:
    if plan.strategy == "document_aware":
        return DocumentAwareChunker(plan, tokenizer)
    return FixedSizeChunker(plan, tokenizer)


def build_chunkers(settings: Settings) -> ConfiguredChunkers:
    return ConfiguredChunkers(
        settings.chunking,
        max_chunk_tokens=settings.max_chunk_tokens,
        embedding_model=settings.models.embedding_model,
    )
