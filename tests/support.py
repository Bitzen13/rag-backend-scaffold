"""Helpers shared by the chunking tests.

Tests measure sizes with `WordTokenizer`, so a size in a test reads as a number of words and the
assertions do not depend on a downloaded tokenizer. `test_tokenizer.py` covers the real one.
"""

from chunking.base import ChunkPlan
from chunking.tokenizer import Tokenizer, WordTokenizer
from extraction.base import DocumentElement, ExtractedDocument
from extraction.elements import place_elements
from settings import ChunkingStrategy

HARD_CAP = 8000


def tokenizer() -> Tokenizer:
    return WordTokenizer()


def plan(
    chunk_size: int = 100,
    chunk_overlap: int = 10,
    *,
    strategy: ChunkingStrategy = "document_aware",
    prose_splitting: str = "paragraph",
    max_chunk_tokens: int = HARD_CAP,
) -> ChunkPlan:
    return ChunkPlan(
        strategy=strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        prose_splitting="sentence" if prose_splitting == "sentence" else "paragraph",
        max_chunk_tokens=max_chunk_tokens,
        tokenizer="words",
    )


def document_of(
    elements: list[DocumentElement], *, source_uri: str = "test.pdf"
) -> ExtractedDocument:
    """An extracted document from hand-built elements, with offsets assigned as a reader does."""
    text, placed = place_elements(elements)
    return ExtractedDocument("file", source_uri, text, {}, placed)


def words(count: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))
