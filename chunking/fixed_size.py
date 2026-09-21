"""Fixed-size chunking: overlapping windows over the document text, ignoring its structure.

Kept for plain text and for anything no reader can find structure in. Sizes are token counts, so
a window is a real budget for the embedding model rather than a word-count approximation.
"""

from chunking.assembly import ChunkAssembler
from chunking.base import ChunkPlan, TextChunk
from chunking.prose import ProseSplitter, WordWindowSplitter, build_prose_splitter
from chunking.tokenizer import Tokenizer
from extraction.base import ExtractedDocument


class FixedSizeChunker:
    """Sliding window over the text. Chunk text is a verbatim slice, so offsets always map back."""

    def __init__(self, plan: ChunkPlan, tokenizer: Tokenizer) -> None:
        self._plan = plan
        self._tokenizer = tokenizer
        self._splitter: ProseSplitter = build_prose_splitter(
            plan.prose_splitting, tokenizer, plan.chunk_overlap
        )

    def chunk(self, document: ExtractedDocument) -> list[TextChunk]:
        return self.split_text(document.text)

    def split_text(self, text: str) -> list[TextChunk]:
        assembler = ChunkAssembler(
            self._tokenizer,
            self._plan.max_chunk_tokens,
            WordWindowSplitter(self._tokenizer, 0),
        )
        for part in self._splitter.split(text, self._plan.chunk_size):
            assembler.add(part.text, part.char_start, part.char_end)
        return assembler.chunks()
