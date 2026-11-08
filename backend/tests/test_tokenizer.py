"""Token counting and the hard cap derived from the embedding model's input limit."""

import logging

import pytest

from app.chunking.tokenizer import (
    INPUT_LIMIT_SAFETY_FRACTION,
    WordTokenizer,
    build_tokenizer,
    default_max_chunk_tokens,
    embedding_input_limit,
)


def test_word_tokenizer_counts_whitespace_words() -> None:
    counter = WordTokenizer()
    assert counter.name == "words"
    assert counter.count("one  two\nthree") == 3
    assert counter.count("   ") == 0


def test_known_models_have_an_input_limit_and_unknown_ones_do_not() -> None:
    assert embedding_input_limit("text-embedding-3-small") == 8191
    assert embedding_input_limit("some-new-model") is None


def test_the_hard_cap_sits_below_the_model_limit() -> None:
    limit = embedding_input_limit("text-embedding-3-large")
    cap = default_max_chunk_tokens("text-embedding-3-large")
    assert limit is not None and cap is not None
    assert cap < limit
    assert cap == int(limit * INPUT_LIMIT_SAFETY_FRACTION)


def test_an_unknown_model_has_no_derived_cap() -> None:
    assert default_max_chunk_tokens("some-new-model") is None


def test_words_is_selected_explicitly_without_loading_a_tokenizer() -> None:
    assert build_tokenizer("words", "text-embedding-3-small").name == "words"


def test_auto_loads_the_embedding_models_tokenizer() -> None:
    counter = build_tokenizer("auto", "text-embedding-3-small")
    if counter.name == "words":
        pytest.skip("tiktoken is unavailable")
    assert counter.name.startswith("tiktoken:")
    # Real tokens, not words: punctuation and sub-words each cost something.
    assert counter.count("Hello world, how are you?") > 5


def test_special_token_strings_in_a_document_are_counted_not_rejected() -> None:
    """A document containing "<|endoftext|>" must not break ingestion."""
    counter = build_tokenizer("auto", "text-embedding-3-small")
    if counter.name == "words":
        pytest.skip("tiktoken is unavailable")
    assert counter.count("before <|endoftext|> after") > 0


def test_a_model_with_no_registered_tokenizer_falls_back_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.chunking.tokenizer"):
        counter = build_tokenizer("auto", "a-model-nobody-has-heard-of")
    if counter.name == "words":
        pytest.skip("tiktoken is unavailable")
    assert counter.name == "tiktoken:cl100k_base"
    assert any("no tokenizer registered" in r.getMessage() for r in caplog.records)
