import pytest

from chunking.factory import build_chunker
from chunking.fixed_size import FixedSizeChunker
from settings import ChunkingConfig


def words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def test_blank_text_yields_no_chunks() -> None:
    assert FixedSizeChunker(10, 2).chunk("  \n\t ") == []


def test_short_text_is_a_single_chunk() -> None:
    chunks = FixedSizeChunker(10, 2).chunk("hello   brave\nworld")
    assert len(chunks) == 1
    assert chunks[0].text == "hello   brave\nworld"
    assert chunks[0].token_count == 3
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len("hello   brave\nworld"))


def test_windows_overlap_and_cover_everything() -> None:
    text = words(25)
    chunks = FixedSizeChunker(10, 3).chunk(text)
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert [c.token_count for c in chunks] == [10, 10, 10, 4]
    assert chunks[0].text.split()[-3:] == chunks[1].text.split()[:3]
    assert chunks[-1].text.split()[-1] == "w24"


def test_no_trailing_chunk_contained_in_previous() -> None:
    chunks = FixedSizeChunker(10, 2).chunk(words(10))
    assert len(chunks) == 1


def test_offsets_map_back_to_source() -> None:
    text = "  alpha beta\n\ngamma delta epsilon  "
    for chunk in FixedSizeChunker(2, 1).chunk(text):
        assert text[chunk.char_start : chunk.char_end] == chunk.text


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (5, 5), (5, -1)])
def test_invalid_sizes_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        FixedSizeChunker(size, overlap)


def test_factory_builds_fixed_size_from_config() -> None:
    chunker = build_chunker(ChunkingConfig(strategy="fixed_size", chunk_size=4, chunk_overlap=1))
    assert [c.token_count for c in chunker.chunk(words(7))] == [4, 4]


def test_factory_rejects_unimplemented_strategy() -> None:
    with pytest.raises(ValueError, match="not implemented"):
        build_chunker(ChunkingConfig(strategy="semantic", chunk_size=4, chunk_overlap=1))
