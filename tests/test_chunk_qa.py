"""The chunk quality report: what it flags, and what it correctly leaves alone."""

from chunking.base import TextChunk
from chunking.qa import (
    DEFECTS,
    ChunkQaReport,
    format_report,
    format_summary,
    reports_as_json,
    review_chunks,
)
from db.models import JsonObject
from extraction.base import ExtractedDocument
from tests.support import plan, words


def document(text: str) -> ExtractedDocument:
    return ExtractedDocument("file", "a.txt", text)


def chunk(
    index: int, text: str, *, start: int = 0, end: int | None = None, meta: JsonObject | None = None
) -> TextChunk:
    """A chunk whose offsets default to covering exactly its own text."""
    return TextChunk(
        index=index,
        text=text,
        token_count=len(text.split()),
        char_start=start,
        char_end=len(text) if end is None else end,
        metadata=meta or {"chunk_type": "prose"},
    )


def kinds(report: ChunkQaReport) -> dict[str, int]:
    return report.issue_counts


def test_clean_chunks_produce_no_issues() -> None:
    text = " ".join(["Alpha beta gamma delta epsilon zeta eta theta iota kappa."] * 4)
    report = review_chunks("a.txt", plan(100, 10), document(text), [chunk(0, text)])
    assert report.issues == ()
    assert "no issues found" in format_report(report)


def test_size_statistics_describe_the_distribution() -> None:
    text = words(30)
    chunks = [chunk(i, words(10)) for i in range(3)]
    report = review_chunks("a.txt", plan(100, 10), document(text), chunks)
    assert report.sizes.count == 3
    assert report.sizes.median == 10
    assert "tokens min/p10/median/mean/p90/max" in format_report(report)


def test_a_chunk_over_the_configured_size_is_a_defect() -> None:
    text = words(20)
    report = review_chunks("a.txt", plan(5, 1), document(text), [chunk(0, text)])
    assert kinds(report)["oversized"] == 1
    assert report.defect_count == 1


def test_a_chunk_over_the_hard_cap_is_reported_separately() -> None:
    text = words(20)
    report = review_chunks(
        "a.txt", plan(5, 1, max_chunk_tokens=10), document(text), [chunk(0, text)]
    )
    assert "over_cap" in kinds(report)
    assert "over the hard token cap" in format_report(report)


def test_a_chunk_with_no_source_content_of_its_own_is_flagged_as_empty() -> None:
    text = "Some real text."
    empty = chunk(0, "Section heading only", start=0, end=0)  # covers none of the source
    report = review_chunks("a.txt", plan(100, 10), document(text), [empty])
    assert kinds(report)["empty"] == 1
    assert set(kinds(report)) & DEFECTS


def test_a_very_small_prose_chunk_is_flagged_but_a_table_row_is_not() -> None:
    text = words(200)
    prose = chunk(0, "tiny", start=0, end=2)
    row = chunk(1, "Name: x", start=3, end=5, meta={"chunk_type": "table_row"})
    report = review_chunks("a.txt", plan(500, 50), document(text), [prose, row])
    undersized = [issue.chunk_index for issue in report.issues if issue.kind == "undersized"]
    assert undersized == [prose.index]


def test_duplicate_chunks_are_reported_once_against_the_original() -> None:
    text = "Repeated text here."
    chunks = [chunk(0, text), chunk(1, text.upper())]
    report = review_chunks("a.txt", plan(100, 10), document(text), chunks)
    assert kinds(report)["duplicate"] == 1
    assert "same as chunk 0" in format_report(report)


def test_a_chunk_starting_inside_a_sentence_is_flagged() -> None:
    text = "The sentence began earlier and continues here."
    start = text.index("continues")
    piece = chunk(0, text[start:], start=start, end=len(text))
    report = review_chunks("a.txt", plan(100, 10), document(text), [piece])
    assert kinds(report)["mid_sentence_start"] == 1


def test_a_chunk_starting_a_new_sentence_is_not_flagged() -> None:
    text = "First sentence. Second sentence follows."
    start = text.index("Second")
    piece = chunk(0, text[start:], start=start, end=len(text))
    report = review_chunks("a.txt", plan(100, 10), document(text), [piece])
    assert "mid_sentence_start" not in kinds(report)


def test_a_chunk_ending_inside_a_word_is_flagged() -> None:
    text = "unbroken continuation"
    piece = chunk(0, text[:12], start=0, end=12)
    report = review_chunks("a.txt", plan(100, 10), document(text), [piece])
    assert kinds(report)["mid_word_end"] == 1


def test_a_chunk_that_is_mostly_a_url_is_flagged() -> None:
    text = "See https://example.com/a/very/long/path/to/somewhere/deep/index.html"
    report = review_chunks("a.txt", plan(100, 10), document(text), [chunk(0, text)])
    assert kinds(report)["mostly_url"] == 1


def test_the_summary_adds_up_every_document() -> None:
    first = review_chunks("a.txt", plan(100, 10), document(words(20)), [chunk(0, words(10))])
    second = review_chunks("b.txt", plan(100, 10), document(words(20)), [chunk(0, words(6))])
    summary = format_summary([first, second])
    assert "2 documents, 2 chunks" in summary
    assert format_summary([]) == "no documents chunked"


def test_the_json_report_carries_the_plan_and_the_totals() -> None:
    body = words(40)
    report = review_chunks("a.txt", plan(100, 10), document(body), [chunk(0, body)])
    payload = reports_as_json([report])
    assert payload["totals"] == {"documents": 1, "chunks": 1, "issues": {}}
    documents = payload["documents"]
    assert isinstance(documents, list)
    entry = documents[0]
    assert entry["plan"]["chunk_size"] == 100
    assert entry["source_uri"] == "a.txt"
