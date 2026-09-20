"""Chunker contract. Implementations are selected from `chunking.strategy` in config.yaml."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int
    char_start: int  # offsets into the source text, for traceability
    char_end: int


class Chunker(Protocol):
    def chunk(self, text: str) -> list[TextChunk]:
        """Split text into ordered chunks; blank text yields no chunks."""
        ...
