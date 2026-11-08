"""Splitting long text: budgets, overlap, and the guarantee that no part exceeds the budget."""

import pytest

from app.chunking.prose import SentenceWindowSplitter, WordWindowSplitter, build_prose_splitter
from app.chunking.tokenizer import build_tokenizer
from tests.support import tokenizer, words


def test_word_windows_fill_the_budget_and_overlap() -> None:
    splitter = WordWindowSplitter(tokenizer(), 2)
    parts = splitter.split(words(10), 5)
    assert parts[0].text.split() == ["w0", "w1", "w2", "w3", "w4"]
    assert parts[1].text.split()[:2] == ["w3", "w4"]  # the overlap
    assert all(p.token_count <= 5 for p in parts)


def test_parts_are_verbatim_slices_of_the_input() -> None:
    text = "  alpha beta gamma   delta epsilon  "
    for part in WordWindowSplitter(tokenizer(), 1).split(text, 2):
        assert text[part.char_start : part.char_end] == part.text


def test_windows_cover_the_whole_text() -> None:
    parts = WordWindowSplitter(tokenizer(), 0).split(words(9), 4)
    assert " ".join(p.text for p in parts) == words(9)


def test_sentence_windows_pack_whole_sentences() -> None:
    text = "One two three. Four five six seven. Eight nine."
    parts = SentenceWindowSplitter(tokenizer(), 0).split(text, 8)
    assert [p.text for p in parts] == ["One two three. Four five six seven.", "Eight nine."]


def test_sentence_overlap_repeats_a_whole_sentence_when_it_fits() -> None:
    text = "AA. BB. CC. DD. EE. FF."
    parts = SentenceWindowSplitter(tokenizer(), 1).split(text, 3)
    assert parts[0].text == "AA. BB. CC."
    assert parts[1].text.startswith("CC.")


def test_a_sentence_longer_than_the_budget_is_split_by_words() -> None:
    text = words(12) + "."
    parts = SentenceWindowSplitter(tokenizer(), 0).split(text, 5)
    assert len(parts) > 1
    assert all(p.token_count <= 5 for p in parts)


def test_empty_and_whitespace_only_text_yields_nothing() -> None:
    assert WordWindowSplitter(tokenizer(), 0).split("", 5) == []
    assert SentenceWindowSplitter(tokenizer(), 1).split("   \n ", 5) == []


def test_a_budget_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="budget"):
        WordWindowSplitter(tokenizer(), 0).split("anything", 0)


def test_overlap_larger_than_the_budget_still_makes_progress() -> None:
    parts = WordWindowSplitter(tokenizer(), 50).split(words(20), 4)
    assert len(parts) > 1
    assert parts[-1].text.split()[-1] == "w19"


def test_the_builder_returns_the_mode_it_was_asked_for() -> None:
    assert isinstance(build_prose_splitter("sentence", tokenizer(), 1), SentenceWindowSplitter)
    assert isinstance(build_prose_splitter("paragraph", tokenizer(), 1), WordWindowSplitter)


def test_no_part_exceeds_the_budget_with_a_real_tokenizer() -> None:
    """The estimate that drives packing is checked against an exact count; this proves it."""
    real = build_tokenizer("auto", "text-embedding-3-small")
    if real.name == "words":
        pytest.skip("tiktoken is unavailable")
    # URLs and punctuation are where per-word estimates drift furthest from the exact count.
    text = " ".join(
        f"See https://example.com/a/very/long/path-{n}/index.html (retrieved 2024-0{n % 9 + 1}-01)."
        for n in range(40)
    )
    for splitter in (SentenceWindowSplitter(real, 10), WordWindowSplitter(real, 10)):
        parts = splitter.split(text, 50)
        assert parts and all(p.token_count <= 50 for p in parts)
