"""Turning finished pieces of text into numbered chunks, under the hard token cap.

Every chunk passes through here, which is the single place the cap is proven rather than assumed:
sizes during chunking are built from per-unit counts, and this recounts the assembled chunk.
"""

import logging

from chunking.base import TextChunk
from chunking.prose import ProseSplitter
from chunking.tokenizer import Tokenizer
from db.models import JsonObject

logger = logging.getLogger(__name__)


class ChunkAssembler:
    """Numbers chunks in order, counts their tokens exactly, and enforces the hard cap."""

    def __init__(
        self, tokenizer: Tokenizer, max_tokens: int, last_resort_splitter: ProseSplitter
    ) -> None:
        self._tokenizer = tokenizer
        self._max_tokens = max_tokens
        self._splitter = last_resort_splitter
        self._chunks: list[TextChunk] = []

    def add(
        self, text: str, char_start: int, char_end: int, metadata: JsonObject | None = None
    ) -> None:
        """Append a chunk, splitting it further if it somehow came out over the cap."""
        if not text.strip():
            return
        tokens = self._tokenizer.count(text)
        if tokens <= self._max_tokens:
            self._append(text, char_start, char_end, tokens, metadata)
            return
        # Not expected: a group's budget already allows for its context line. Splitting here keeps
        # the promise that no chunk is ever handed to the embedding model to be truncated.
        logger.warning(
            "a chunk came out at %d tokens, over the %d-token cap; splitting it further",
            tokens,
            self._max_tokens,
        )
        for part in self._splitter.split(text, self._max_tokens):
            self._append(part.text, char_start, char_end, part.token_count, metadata)

    def _append(
        self, text: str, char_start: int, char_end: int, tokens: int, metadata: JsonObject | None
    ) -> None:
        self._chunks.append(
            TextChunk(
                index=len(self._chunks),
                text=text,
                token_count=tokens,
                char_start=char_start,
                char_end=char_end,
                metadata=dict(metadata) if metadata else {},
            )
        )

    def chunks(self) -> list[TextChunk]:
        return self._chunks
