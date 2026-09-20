import re

from chunking.base import TextChunk

_WORD = re.compile(r"\S+")


class FixedSizeChunker:
    """Sliding window over whitespace-delimited words.

    Sizes are counted in words, a cheap tokenizer-free approximation of tokens. Chunk text is a
    verbatim slice of the source, so offsets always map back to it.
    """

    def __init__(self, chunk_size: int, chunk_overlap: int) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")
        self._size = chunk_size
        self._step = chunk_size - chunk_overlap

    def chunk(self, text: str) -> list[TextChunk]:
        words = [m.span() for m in _WORD.finditer(text)]
        chunks: list[TextChunk] = []
        for first in range(0, len(words), self._step):
            window = words[first : first + self._size]
            start, end = window[0][0], window[-1][1]
            chunks.append(
                TextChunk(
                    index=len(chunks),
                    text=text[start:end],
                    token_count=len(window),
                    char_start=start,
                    char_end=end,
                )
            )
            if first + self._size >= len(words):
                break
        return chunks
