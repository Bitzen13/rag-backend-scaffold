from chunking.document_aware import DocumentAwareChunker
from extraction.base import DocumentElement, ElementKind, ExtractedDocument

TITLE = "List of the largest known stars in the Milky Way"
HEADER = ("Star name", "Solar radius (R☉)", "Method", "Notes")


def document_of(elements: list[DocumentElement]) -> ExtractedDocument:
    """Joins element texts and gives each its offsets, like the PDF reader does."""
    placed: list[DocumentElement] = []
    position = 0
    for element in elements:
        end = position + len(element.text)
        placed.append(
            DocumentElement(
                element.kind,
                element.text,
                element.page,
                element.section,
                element.table,
                element.fields,
                position,
                end,
            )
        )
        position = end + 1
    text = "\n".join(e.text for e in placed)
    return ExtractedDocument("file", "stars.pdf", text, {}, tuple(placed))


def row(*values: str | None) -> DocumentElement:
    fields = tuple((h, v) for h, v in zip(HEADER, values, strict=False) if v)
    text = " | ".join(f"{h}: {v}" for h, v in fields)
    return DocumentElement(ElementKind.TABLE_ROW, text, 1, "Milky Way", TITLE, fields)


def build(rows: list[DocumentElement]) -> ExtractedDocument:
    heading = DocumentElement(ElementKind.HEADING, "Milky Way", 1, "Milky Way")
    intro = DocumentElement(ElementKind.PARAGRAPH, "Intro text before the table.", 1, "Milky Way")
    return document_of([heading, intro, *rows])


def test_every_table_row_is_its_own_chunk_with_context() -> None:
    document = build(
        [
            row("VY Canis Majoris", "1,420±120", "AD", "An extreme hypergiant."),
            row("S Persei", "1,364 ± 6", "AD", None),
        ]
    )

    chunks = DocumentAwareChunker(100, 10).chunk(document)

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
    chunks = DocumentAwareChunker(100, 10).chunk(document)
    found = next(c for c in chunks if "S Persei" in c.text)
    assert document.text[found.char_start : found.char_end] == (
        "Star name: S Persei | Solar radius (R☉): 1,364 | Method: AD"
    )


def test_prose_is_kept_under_its_heading_and_not_dropped() -> None:
    chunks = DocumentAwareChunker(100, 10).chunk(build([row("A", "1", "AD", None)]))
    prose = [c for c in chunks if c.metadata["chunk_type"] == "prose"]
    assert [c.text for c in prose] == ["Milky Way\nIntro text before the table."]
    assert prose[0].metadata["section"] == "Milky Way"


def make_paragraphs(*texts: str, section: str = "S") -> ExtractedDocument:
    return document_of([DocumentElement(ElementKind.PARAGRAPH, t, 1, section) for t in texts])


def test_paragraphs_are_packed_up_to_chunk_size() -> None:
    document = make_paragraphs("one two three", "four five six", "seven eight nine")
    chunks = DocumentAwareChunker(6, 1).chunk(document)
    assert [c.text for c in chunks] == ["S\none two three\n\nfour five six", "S\nseven eight nine"]


def test_oversized_paragraph_is_split_and_keeps_heading() -> None:
    document = make_paragraphs(" ".join(f"w{i}" for i in range(10)))
    chunks = DocumentAwareChunker(4, 1).chunk(document)
    assert len(chunks) > 1
    assert all(c.text.startswith("S\n") for c in chunks)
    assert all(c.token_count <= 5 for c in chunks)  # 4 words + the heading


def test_oversized_row_is_split_repeating_context_and_name() -> None:
    long_note = " ".join(f"n{i}" for i in range(30))
    document = build([row("Big Star", "1", "AD", long_note)])
    parts = [
        c
        for c in DocumentAwareChunker(12, 2).chunk(document)
        if c.metadata["chunk_type"] == "table_row"
    ]
    assert len(parts) > 1
    assert all(p.text.startswith(f"{TITLE}\nBig Star (part ") for p in parts)


def test_document_without_structure_falls_back_to_fixed_size() -> None:
    document = ExtractedDocument("file", "a.txt", " ".join(f"w{i}" for i in range(9)))
    chunks = DocumentAwareChunker(4, 1).chunk(document)
    assert [c.token_count for c in chunks] == [4, 4, 3]
    assert all(c.metadata == {} for c in chunks)
