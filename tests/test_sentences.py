"""Sentence segmentation, including the scripts that do not work like English."""

import pytest

from app.chunking.sentences import sentence_spans, word_spans


def sentences(text: str) -> list[str]:
    return [text[start:end] for start, end in sentence_spans(text)]


def test_plain_sentences_split_on_their_terminators() -> None:
    assert sentences("One two. Three four! Five six? Seven.") == [
        "One two.",
        "Three four!",
        "Five six?",
        "Seven.",
    ]


def test_spans_are_verbatim_slices_in_order() -> None:
    text = "  First one.   Second one.  "
    spans = sentence_spans(text)
    assert [text[a:b] for a, b in spans] == ["First one.", "Second one."]
    assert spans == sorted(spans)


@pytest.mark.parametrize(
    "text",
    [
        "Values reached 3.14 in the test.",
        "See Fig. 4 for the detail.",
        "Dr. Smith agrees with Mr. Jones.",
        "J. R. R. Tolkien wrote it.",
        "Use a hammer, e.g. a small one.",
        "Erster Satz bzw. Teil davon.",
    ],
)
def test_a_stop_that_does_not_end_a_sentence_is_not_a_boundary(text: str) -> None:
    assert sentences(text) == [text]


def test_a_blank_line_always_ends_a_sentence() -> None:
    assert sentences("No terminator here\n\nAnd the next block") == [
        "No terminator here",
        "And the next block",
    ]


def test_closing_punctuation_stays_with_its_sentence() -> None:
    assert sentences('He said "stop." Then left.') == ['He said "stop."', "Then left."]


def test_chinese_breaks_without_a_following_space() -> None:
    assert sentences("你好世界。这是第二句！") == [
        "你好世界。",
        "这是第二句！",
    ]


def test_devanagari_danda_ends_a_sentence() -> None:
    text = "पहला वाक्य। दूसरा वाक्य।"
    assert len(sentences(text)) == 2


def test_arabic_question_mark_ends_a_sentence() -> None:
    text = "هذه جملة؟ نعم."
    assert len(sentences(text)) == 2


def test_text_with_no_terminator_at_all_is_one_sentence() -> None:
    thai = "สวัสดีครับนี่"
    assert sentences(thai) == [thai]


def test_empty_text_has_no_sentences() -> None:
    assert sentence_spans("") == []
    assert sentence_spans("   \n  ") == []


def test_word_spans_cover_every_word() -> None:
    text = " alpha  beta\ngamma "
    assert [text[a:b] for a, b in word_spans(text)] == ["alpha", "beta", "gamma"]
