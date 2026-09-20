from chunking.base import Chunker
from chunking.fixed_size import FixedSizeChunker
from settings import ChunkingConfig


def build_chunker(config: ChunkingConfig) -> Chunker:
    if config.strategy == "fixed_size":
        return FixedSizeChunker(config.chunk_size, config.chunk_overlap)
    raise ValueError(f"chunking strategy {config.strategy!r} is not implemented yet")
