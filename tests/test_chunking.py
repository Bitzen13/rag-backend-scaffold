import logging

import pytest

from chunking.base import ChunkingStrategyError
from chunking.document_aware import DocumentAwareChunker
from chunking.factory import ConfiguredChunkers
from chunking.fixed_size import FixedSizeChunker
from extraction.base import ExtractedDocument
from settings import ChunkingConfig


def words(n: int) -> str:
    return " ".join(f"w{i}" for i in range(n))


def doc(text: str, uri: str = "x.txt") -> ExtractedDocument:
    return ExtractedDocument("file", uri, text)


def test_blank_text_yields_no_chunks() -> None:
    assert FixedSizeChunker(10, 2).chunk(doc("  \n\t ")) == []


def test_short_text_is_a_single_chunk() -> None:
    text = "hello   brave\nworld"
    chunks = FixedSizeChunker(10, 2).chunk(doc(text))
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].token_count == 3
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len(text))


def test_windows_overlap_and_cover_everything() -> None:
    chunks = FixedSizeChunker(10, 3).chunk(doc(words(25)))
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert [c.token_count for c in chunks] == [10, 10, 10, 4]
    assert chunks[0].text.split()[-3:] == chunks[1].text.split()[:3]
    assert chunks[-1].text.split()[-1] == "w24"


def test_no_trailing_chunk_contained_in_previous() -> None:
    assert len(FixedSizeChunker(10, 2).chunk(doc(words(10)))) == 1


def test_offsets_map_back_to_source() -> None:
    text = "  alpha beta\n\ngamma delta epsilon  "
    for chunk in FixedSizeChunker(2, 1).chunk(doc(text)):
        assert text[chunk.char_start : chunk.char_end] == chunk.text


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (5, 5), (5, -1)])
def test_invalid_sizes_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        FixedSizeChunker(size, overlap)


# --- per-document strategy selection ---------------------------------------------------------


def config(documents: dict[str, str]) -> ChunkingConfig:
    return ChunkingConfig.model_validate(
        {"chunk_size": 4, "chunk_overlap": 1, "documents": documents}
    )


def test_each_document_uses_its_own_configured_strategy() -> None:
    chunkers = ConfiguredChunkers(config({"a.txt": "fixed_size", "b.pdf": "document_aware"}))
    assert isinstance(chunkers.for_document("a.txt"), FixedSizeChunker)
    assert isinstance(chunkers.for_document("b.pdf"), DocumentAwareChunker)
    assert chunkers.strategy_for("b.pdf") == "document_aware"


def test_unlisted_document_has_no_default_strategy() -> None:
    chunkers = ConfiguredChunkers(config({"a.txt": "fixed_size"}))
    with pytest.raises(ChunkingStrategyError, match=r"chunking\.documents"):
        chunkers.for_document("other.txt")


def test_unknown_strategy_rejected_by_config() -> None:
    with pytest.raises(ValueError, match="documents"):
        config({"a.txt": "semantic"})


# --- ingestion logging -----------------------------------------------------------------------


def test_chunk_logging_summarises_at_info_and_details_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ingestion.pipeline import log_chunks

    chunks = FixedSizeChunker(4, 1).chunk(doc(words(7)))

    with caplog.at_level(logging.DEBUG, logger="ingestion.pipeline"):
        log_chunks("a.txt", "FixedSizeChunker", chunks)

    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert info == [
        "chunked a.txt with FixedSizeChunker: 2 chunks, words min/avg/max = 4/4/4, types: text=2"
    ]
    assert len(debug) == 2
    assert "chunk 0: 4 words, chars 0-11" in debug[0]
    assert "w0 w1 w2 w3" in debug[0]
