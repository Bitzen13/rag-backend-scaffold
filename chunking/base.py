"""Chunker contract. The strategy for each document is chosen in `chunking.documents`."""

from dataclasses import dataclass, field
from typing import Protocol

from db.models import JsonObject
from extraction.base import ExtractedDocument


class ChunkingStrategyError(ValueError):
    """A document has no (or an unusable) chunking strategy configured."""


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int
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
