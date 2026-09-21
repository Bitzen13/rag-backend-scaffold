from chunking.base import Chunker, ChunkingStrategyError
from chunking.document_aware import DocumentAwareChunker
from chunking.fixed_size import FixedSizeChunker
from settings import ChunkingConfig, ChunkingStrategy


class ConfiguredChunkers:
    """Resolves each document's chunker from `chunking.documents`; there is no default."""

    def __init__(self, config: ChunkingConfig) -> None:
        self._documents = config.documents
        self._chunkers: dict[ChunkingStrategy, Chunker] = {
            "fixed_size": FixedSizeChunker(config.chunk_size, config.chunk_overlap),
            "document_aware": DocumentAwareChunker(config.chunk_size, config.chunk_overlap),
        }

    def strategy_for(self, source_uri: str) -> ChunkingStrategy:
        strategy = self._documents.get(source_uri)
        if strategy is None:
            raise ChunkingStrategyError(
                f"no chunking strategy set for {source_uri!r}: add it under chunking.documents "
                f"in config.yaml (one of: {', '.join(self._chunkers)})"
            )
        return strategy

    def for_document(self, source_uri: str) -> Chunker:
        return self._chunkers[self.strategy_for(source_uri)]
