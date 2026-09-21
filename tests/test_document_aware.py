"""The chunking rules, one per element kind, on hand-built elements.

Elements rather than files on purpose: these are the rules every reader's output goes through, so
they are tested against the element model itself. `test_readers.py` covers producing elements
from real files.
"""

from chunking.document_aware import DocumentAwareChunker
from chunking.tokenizer import build_tokenizer
from extraction.base import DocumentElement, ElementKind, ExtractedDocument
from tests.support import document_of, plan, tokenizer, words

TITLE = "List of the largest known stars in the Milky Way"
HEADER = ("Star name", "Solar radius (R☉)", "Method", "Notes")


def chunker(
    chunk_size: int = 100, chunk_overlap: int = 10, prose: str = "paragraph"
) -> DocumentAwareChunker:
    return DocumentAwareChunker(plan(chunk_size, chunk_overlap, prose_splitting=prose), tokenizer())


def row(*values: str | None) -> DocumentElement:
    fields = tuple((h, v) for h, v in zip(HEADER, values, strict=False) if v)
    text = " | ".join(f"{h}: {v}" for h, v in fields)
    return DocumentElement(
        kind=ElementKind.TABLE_ROW,
        text=text,
        page=1,
        section="Milky Way",
        table=TITLE,
        fields=fields,
    )


def paragraph(text: str, section: str = "S", page: int = 1) -> DocumentElement:
    return DocumentElement(ElementKind.PARAGRAPH, text, page, section)


def build(rows: list[DocumentElement]) -> ExtractedDocument:
    heading = DocumentElement(ElementKind.HEADING, "Milky Way", 1, "Milky Way")
    intro = paragraph("Intro text before the table.", "Milky Way")
    return document_of([heading, intro, *rows])


# --- table rows ------------------------------------------------------------------------------


def test_every_table_row_is_its_own_chunk_with_context() -> None:
    document = build(
        [
            row("VY Canis Majoris", "1,420±120", "AD", "An extreme hypergiant."),
            row("S Persei", "1,364 ± 6", "AD", None),
        ]
    )

    chunks = chunker().chunk(document)

    rows = [c for c in chunks if c.metadata["chunk_type"] == "table_row"]
    assert len(rows) == 2
    assert rows[0].text == (
        f"{TITLE}\n"
        "Star name: VY Canis Majoris | Solar radius (R☉): 1,420±120 | Method: AD | "
        "Notes: An extreme hypergiant."
    )
    assert rows[1].text.endswith("Star name: S Persei | Solar radius (R☉): 1,364 ± 6 | Method: AD")
    assert rows[0].metadata == {
        "chunk_type": "table_row",
        "table": TITLE,
        "section": "Milky Way",
        "page": 1,
        "row_name": "VY Canis Majoris",
    }


def test_row_offsets_point_at_the_row_in_document_text() -> None:
    document = build([row("S Persei", "1,364", "AD", None)])
    found = next(c for c in chunker().chunk(document) if "S Persei" in c.text)
    assert document.text[found.char_start : found.char_end] == (
        "Star name: S Persei | Solar radius (R☉): 1,364 | Method: AD"
    )


def test_oversized_row_is_split_repeating_context_and_name() -> None:
    document = build([row("Big Star", "1", "AD", words(80, "n"))])
    parts = [c for c in chunker(60, 5).chunk(document) if c.metadata["chunk_type"] == "table_row"]
    assert len(parts) > 1
    assert all(p.text.startswith(f"{TITLE}\nBig Star (part ") for p in parts)
    assert all(p.token_count <= 60 for p in parts)


# --- prose -----------------------------------------------------------------------------------


def test_prose_is_kept_under_its_heading_and_not_dropped() -> None:
    chunks = chunker().chunk(build([row("A", "1", "AD", None)]))
    prose = [c for c in chunks if c.metadata["chunk_type"] == "prose"]
    assert [c.text for c in prose] == ["Milky Way\nIntro text before the table."]
    assert prose[0].metadata["section"] == "Milky Way"


def test_paragraphs_are_packed_up_to_chunk_size() -> None:
    document = document_of(
        [paragraph("one two three"), paragraph("four five six"), paragraph("seven eight nine")]
    )
    chunks = chunker(10, 1).chunk(document)
    assert [c.text for c in chunks] == ["S\none two three\n\nfour five six", "S\nseven eight nine"]


def test_paragraphs_in_different_sections_are_not_mixed() -> None:
    document = document_of([paragraph("alpha", "One"), paragraph("beta", "Two")])
    assert [c.text for c in chunker().chunk(document)] == ["One\nalpha", "Two\nbeta"]


def test_oversized_paragraph_is_split_and_keeps_its_heading() -> None:
    document = document_of([paragraph(words(40))])
    chunks = chunker(12, 1).chunk(document)
    assert len(chunks) > 1
    assert all(c.text.startswith("S\n") for c in chunks)
    assert all(c.token_count <= 12 for c in chunks)


def test_context_is_counted_against_the_budget_not_added_to_it() -> None:
    deep = " > ".join(["Section"] * 3)
    document = document_of([paragraph(words(40), deep)])
    assert all(c.token_count <= 15 for c in chunker(15, 0).chunk(document))


def test_a_heading_path_too_long_for_the_budget_is_trimmed_to_the_nearest_part() -> None:
    deep = " > ".join(words(30, "h").split())
    document = document_of([paragraph("short body", deep)])
    (chunk,) = chunker(12, 0).chunk(document)
    assert chunk.token_count <= 12
    assert "h29" in chunk.text  # the nearest heading survives, the outermost are dropped


def test_sentence_mode_never_cuts_inside_a_sentence() -> None:
    body = "Alpha beta gamma delta. Epsilon zeta eta theta. Iota kappa lambda mu."
    document = document_of([paragraph(body)])
    chunks = chunker(12, 0, prose="sentence").chunk(document)
    bodies = [c.text.removeprefix("S\n") for c in chunks]
    assert bodies == [
        "Alpha beta gamma delta. Epsilon zeta eta theta.",
        "Iota kappa lambda mu.",
    ]


# --- lists -----------------------------------------------------------------------------------


def item(text: str, lead_in: str = "Do the following:", section: str = "S") -> DocumentElement:
    return DocumentElement(ElementKind.LIST_ITEM, text, 1, section, lead_in=lead_in)


def test_list_items_are_grouped_under_their_lead_in() -> None:
    document = document_of([item("first step"), item("second step"), item("third step")])
    (chunk,) = chunker().chunk(document)
    assert chunk.text == "S / Do the following:\nfirst step\nsecond step\nthird step"
    assert chunk.metadata["chunk_type"] == "list"


def test_lists_with_different_lead_ins_are_separate_chunks() -> None:
    document = document_of([item("a", "First list:"), item("b", "Second list:")])
    chunks = chunker().chunk(document)
    assert [c.text for c in chunks] == ["S / First list:\na", "S / Second list:\nb"]


def test_a_long_list_splits_and_repeats_the_lead_in() -> None:
    document = document_of([item(f"step {n} here") for n in range(24)])
    chunks = chunker(40, 0).chunk(document)
    assert len(chunks) > 1
    assert all(c.text.startswith("S / Do the following:\n") for c in chunks)


# --- code ------------------------------------------------------------------------------------


def test_a_code_block_stays_whole_and_is_never_merged() -> None:
    code = DocumentElement(
        ElementKind.CODE, "def f():\n    return 1", 1, "Setup", language="python"
    )
    document = document_of(
        [paragraph("Before the code.", "Setup"), code, paragraph("After.", "Setup")]
    )
    chunks = chunker().chunk(document)
    assert [c.metadata["chunk_type"] for c in chunks] == ["prose", "code", "prose"]
    assert chunks[1].text == "Setup / python code\ndef f():\n    return 1"
    assert chunks[1].metadata["language"] == "python"


# --- questions and answers -------------------------------------------------------------------


def test_a_question_stays_with_its_answer() -> None:
    document = document_of(
        [
            DocumentElement(ElementKind.QUESTION, "What port does it use?", 1, "FAQ"),
            DocumentElement(ElementKind.ANSWER, "Port 8000 by default.", 1, "FAQ"),
        ]
    )
    (chunk,) = chunker().chunk(document)
    assert chunk.text == "FAQ / What port does it use?\nPort 8000 by default."
    assert chunk.metadata["chunk_type"] == "qa"


def test_a_long_answer_repeats_its_question_on_every_part() -> None:
    document = document_of(
        [
            DocumentElement(ElementKind.QUESTION, "How?", 1, "FAQ"),
            DocumentElement(ElementKind.ANSWER, words(40, "a"), 1, "FAQ"),
        ]
    )
    chunks = chunker(12, 0).chunk(document)
    assert len(chunks) > 1
    assert all(c.text.startswith("FAQ / How?\n") for c in chunks)


def test_a_second_question_starts_a_new_chunk() -> None:
    document = document_of(
        [
            DocumentElement(ElementKind.QUESTION, "First?", 1, "FAQ"),
            DocumentElement(ElementKind.ANSWER, "Yes.", 1, "FAQ"),
            DocumentElement(ElementKind.QUESTION, "Second?", 1, "FAQ"),
            DocumentElement(ElementKind.ANSWER, "No.", 1, "FAQ"),
        ]
    )
    assert [c.text for c in chunker().chunk(document)] == [
        "FAQ / First?\nYes.",
        "FAQ / Second?\nNo.",
    ]


# --- transcripts -----------------------------------------------------------------------------


def turn(speaker: str, text: str, seconds: float | None) -> DocumentElement:
    return DocumentElement(
        ElementKind.SPEAKER_TURN, text, 1, "Call", speaker=speaker, time_seconds=seconds
    )


def test_turns_within_the_time_window_share_a_chunk_and_keep_their_speakers() -> None:
    document = document_of([turn("Ann", "Hello there.", 0.0), turn("Bob", "Hi Ann.", 5.0)])
    (chunk,) = chunker().chunk(document)
    assert chunk.text == "Call\nAnn: Hello there.\nBob: Hi Ann."
    assert chunk.metadata["speakers"] == ["Ann", "Bob"]
    assert chunk.metadata["time_start"] == 0.0


def test_a_long_gap_starts_a_new_chunk() -> None:
    document = document_of(
        [turn("Ann", "Hello.", 0.0), turn("Bob", "Hi.", 10.0), turn("Bob", "Much later.", 900.0)]
    )
    chunks = chunker().chunk(document)
    assert [c.text for c in chunks] == ["Call\nAnn: Hello.\nBob: Hi.", "Call\nBob: Much later."]


def test_turns_without_timestamps_are_limited_only_by_size() -> None:
    document = document_of([turn("Ann", f"line {n} of talking", None) for n in range(10)])
    chunks = chunker(15, 0).chunk(document)
    assert len(chunks) > 1
    assert all(c.token_count <= 15 for c in chunks)


# --- messages --------------------------------------------------------------------------------


def test_each_message_is_one_chunk() -> None:
    document = document_of(
        [
            DocumentElement(
                ElementKind.MESSAGE, "From: a\n\nFirst mail.", 1, "Subject A", speaker="a"
            ),
            DocumentElement(
                ElementKind.MESSAGE, "From: b\n\nSecond mail.", 2, "Subject A", speaker="b"
            ),
        ]
    )
    chunks = chunker().chunk(document)
    assert len(chunks) == 2
    assert chunks[0].metadata["chunk_type"] == "message"
    assert chunks[0].metadata["speakers"] == ["a"]


# --- fallbacks and limits --------------------------------------------------------------------


def test_document_without_structure_falls_back_to_fixed_size() -> None:
    document = ExtractedDocument("file", "a.txt", words(9))
    chunks = chunker(4, 1).chunk(document)
    assert [c.token_count for c in chunks] == [4, 4, 3]
    assert all(c.metadata == {} for c in chunks)


def test_empty_document_yields_no_chunks() -> None:
    assert chunker().chunk(document_of([])) == []


def test_no_chunk_exceeds_the_hard_cap() -> None:
    real = build_tokenizer("auto", "text-embedding-3-small")
    document = document_of([paragraph("中" * 400), paragraph(words(200))])
    chunks = DocumentAwareChunker(plan(60, 5, max_chunk_tokens=60), real).chunk(document)
    assert chunks and all(c.token_count <= 60 for c in chunks)


def test_unicode_and_very_short_documents_are_handled() -> None:
    document = document_of([paragraph("你好世界。", "星")])
    (chunk,) = chunker().chunk(document)
    assert chunk.text == "星\n你好世界。"
