import logging

import pytest

from app.chunking.base import ChunkingStrategyError
from app.chunking.document_aware import DocumentAwareChunker
from app.chunking.factory import ConfiguredChunkers
from app.chunking.fixed_size import FixedSizeChunker
from app.chunking.tokenizer import build_tokenizer
from app.extraction.base import ExtractedDocument
from app.settings import ChunkingConfig
from tests.support import plan, tokenizer, words


def doc(text: str, uri: str = "x.txt") -> ExtractedDocument:
    return ExtractedDocument("file", uri, text)


def chunker(chunk_size: int, chunk_overlap: int, prose: str = "paragraph") -> FixedSizeChunker:
    return FixedSizeChunker(
        plan(chunk_size, chunk_overlap, strategy="fixed_size", prose_splitting=prose), tokenizer()
    )


def test_blank_text_yields_no_chunks() -> None:
    assert chunker(10, 2).chunk(doc("  \n\t ")) == []


def test_short_text_is_a_single_chunk() -> None:
    text = "hello   brave\nworld"
    chunks = chunker(10, 2).chunk(doc(text))
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].token_count == 3
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len(text))


def test_windows_overlap_and_cover_everything() -> None:
    chunks = chunker(10, 3).chunk(doc(words(25)))
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert [c.token_count for c in chunks] == [10, 10, 10, 4]
    assert chunks[0].text.split()[-3:] == chunks[1].text.split()[:3]
    assert chunks[-1].text.split()[-1] == "w24"


def test_no_trailing_chunk_contained_in_previous() -> None:
    assert len(chunker(10, 2).chunk(doc(words(10)))) == 1


def test_offsets_map_back_to_source() -> None:
    text = "  alpha beta\n\ngamma delta epsilon  "
    for chunk in chunker(2, 1).chunk(doc(text)):
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_no_chunk_exceeds_the_size_it_was_given() -> None:
    chunks = chunker(7, 2).chunk(doc(words(60)))
    assert chunks and all(c.token_count <= 7 for c in chunks)


def test_text_with_no_word_breaks_is_still_split_within_budget() -> None:
    """CJK and long encoded strings have no whitespace to split on, so they split by character.

    This needs a tokenizer that sees more than one token in unbroken text, which word counting by
    definition does not.
    """
    real = build_tokenizer("auto", "text-embedding-3-small")
    if real.name == "words":
        pytest.skip("tiktoken is unavailable, so there is no per-character budget to test")
    text = "中" * 200
    chunks = FixedSizeChunker(plan(20, 0, strategy="fixed_size"), real).chunk(doc(text))
    assert len(chunks) > 1
    assert all(c.token_count <= 20 for c in chunks)
    assert "".join(c.text for c in chunks) == text


def test_sentence_mode_cuts_between_sentences() -> None:
    text = "One two three. Four five six seven. Eight nine ten."
    chunks = chunker(8, 0, prose="sentence").chunk(doc(text))
    assert [c.text for c in chunks] == ["One two three. Four five six seven.", "Eight nine ten."]


def test_invalid_sizes_rejected_by_config() -> None:
    with pytest.raises(ValueError, match="chunk_overlap"):
        ChunkingConfig.model_validate({"chunk_size": 5, "chunk_overlap": 5})


# --- per-document strategy and options -------------------------------------------------------


def config(documents: dict[str, object], **extra: object) -> ChunkingConfig:
    return ChunkingConfig.model_validate(
        {"chunk_size": 4, "chunk_overlap": 1, "documents": documents, **extra}
    )


def chunkers(documents: dict[str, object], **extra: object) -> ConfiguredChunkers:
    return ConfiguredChunkers(
        config(documents, **extra),
        max_chunk_tokens=8000,
        embedding_model="text-embedding-3-small",
    )


def test_each_document_uses_its_own_configured_strategy() -> None:
    resolved = chunkers({"a.txt": "fixed_size", "b.pdf": "document_aware"})
    assert isinstance(resolved.for_document("a.txt"), FixedSizeChunker)
    assert isinstance(resolved.for_document("b.pdf"), DocumentAwareChunker)
    assert resolved.strategy_for("b.pdf") == "document_aware"


def test_unlisted_document_has_no_default_strategy() -> None:
    with pytest.raises(ChunkingStrategyError, match=r"chunking\.documents"):
        chunkers({"a.txt": "fixed_size"}).for_document("other.txt")


def test_unknown_strategy_rejected_by_config() -> None:
    with pytest.raises(ValueError, match="documents"):
        config({"a.txt": "semantic"})


def test_per_document_options_override_the_global_ones() -> None:
    resolved = chunkers(
        {
            "plain.md": "fixed_size",
            "report.pdf": {
                "strategy": "document_aware",
                "chunk_size": 40,
                "chunk_overlap": 5,
                "prose_splitting": "sentence",
            },
        },
        prose_splitting="paragraph",
    )
    default = resolved.plan_for("plain.md")
    special = resolved.plan_for("report.pdf")
    assert (default.chunk_size, default.chunk_overlap) == (4, 1)
    assert default.prose_splitting == "paragraph"
    assert (special.chunk_size, special.chunk_overlap) == (40, 5)
    assert special.prose_splitting == "sentence"


def test_plan_records_what_a_source_was_chunked_with() -> None:
    recorded = chunkers({"a.pdf": "document_aware"}).plan_for("a.pdf").as_metadata()
    assert recorded["strategy"] == "document_aware"
    assert recorded["chunk_size"] == 4
    assert recorded["max_chunk_tokens"] == 8000
    assert str(recorded["tokenizer"]).startswith(("tiktoken:", "words"))


def test_one_chunker_is_reused_for_documents_with_the_same_plan() -> None:
    resolved = chunkers({"a.pdf": "document_aware", "b.pdf": "document_aware"})
    assert resolved.for_document("a.pdf") is resolved.for_document("b.pdf")


# --- ingestion logging -----------------------------------------------------------------------


def test_chunk_logging_summarises_at_info_and_details_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.ingestion.pipeline import log_chunks

    document_plan = plan(4, 1, strategy="fixed_size")
    chunks = FixedSizeChunker(document_plan, tokenizer()).chunk(doc(words(7)))

    with caplog.at_level(logging.DEBUG, logger="app.ingestion.pipeline"):
        log_chunks("a.txt", document_plan, chunks)

    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(info) == 1
    assert "chunked a.txt [fixed_size, size=4, overlap=1" in info[0]
    assert "2 chunks, tokens min/avg/max = 4/4/4, types: text=2" in info[0]
    assert len(debug) == 2
    assert "chunk 0: 4 tokens, chars 0-11" in debug[0]
    assert "w0 w1 w2 w3" in debug[0]
