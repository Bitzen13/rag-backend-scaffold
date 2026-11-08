"""Chunker contract. The strategy and options for each document come from `chunking.documents`."""

from dataclasses import dataclass, field
from typing import Protocol

from app.chunking.prose import ProseSplitting
from app.db.models import JsonObject
from app.extraction.base import ExtractedDocument
from app.settings import ChunkingStrategy


class ChunkingStrategyError(ValueError):
    """A document has no (or an unusable) chunking strategy configured."""


@dataclass(frozen=True)
class ChunkPlan:
    """The resolved chunking settings for one document.

    Recorded on the source row at ingestion (`sources.chunking`) so that a later run can tell
    that the configuration changed and the document needs re-chunking, and so that a stored
    chunk's size is always interpretable.
    """

    strategy: ChunkingStrategy
    chunk_size: int
    chunk_overlap: int
    prose_splitting: ProseSplitting
    max_chunk_tokens: int
    tokenizer: str

    def as_metadata(self) -> JsonObject:
        return {
            "strategy": self.strategy,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "prose_splitting": self.prose_splitting,
            "max_chunk_tokens": self.max_chunk_tokens,
            "tokenizer": self.tokenizer,
        }

    def describe(self) -> str:
        return (
            f"{self.strategy}, size={self.chunk_size}, overlap={self.chunk_overlap}, "
            f"prose={self.prose_splitting}, tokenizer={self.tokenizer}"
        )


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int  # exact count from the plan's tokenizer
    char_start: int  # offsets into the source text, for traceability
    char_end: int
    metadata: JsonObject = field(default_factory=dict)  # e.g. chunk_type, table, section, page


class Chunker(Protocol):
    def chunk(self, document: ExtractedDocument) -> list[TextChunk]:
        """Split a document into ordered chunks; a document with no text yields no chunks."""
        ...


class ChunkerResolver(Protocol):
    def for_document(self, source_uri: str) -> Chunker:
        """Return the chunker configured for this document, or raise ChunkingStrategyError."""
        ...

    def plan_for(self, source_uri: str) -> ChunkPlan:
        """Return the resolved plan for this document, or raise ChunkingStrategyError."""
        ...
