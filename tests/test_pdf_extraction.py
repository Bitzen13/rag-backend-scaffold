"""The PDF reader must work out structure per document, not assume one document's typography."""

from extraction.base import DocumentElement, ElementKind
from extraction.pdf import PdfOptions
from extraction.pdf.builder import StructureBuilder
from extraction.pdf.clean import TextCleaner
from extraction.pdf.model import Line, PageFlow, TableBlock
from extraction.pdf.stats import DocStats

# --- text cleaning ---------------------------------------------------------------------------


def cleaner(
    *,
    strip_patterns: tuple[str, ...] = (),
    truncate_patterns: tuple[str, ...] = (),
) -> TextCleaner:
    return TextCleaner(
        PdfOptions(strip_patterns=strip_patterns, truncate_patterns=truncate_patterns)
    )


def test_cleaner_collapses_whitespace_and_handles_empty() -> None:
    assert cleaner().clean("  a \n b\t c ") == "a b c"
    assert cleaner().clean(None) == ""
    assert cleaner().clean("<") == ""  # stray punctuation is not a value


def test_strip_patterns_remove_matches_anywhere() -> None:
    clean = cleaner(strip_patterns=(r"\[\d+\]",))
    assert clean.clean("1,540[12][13] ± 77[12]") == "1,540 ± 77"


def test_truncate_patterns_drop_everything_from_the_first_match() -> None:
    clean = cleaner(truncate_patterns=("Template:",))
    assert clean.clean("51.18Template:+-[97]") == "51.18"
    assert clean.clean("Template:Val") == ""
    assert clean.clean("Solar radius (Template:Solar radius)") == "Solar radius"  # dangling "("


# --- structure from a hand-built page --------------------------------------------------------

BODY_SIZE = 11.0
STATS = DocStats(body_size=BODY_SIZE, line_pitch=14.0, heading_sizes=(16.0,), furniture=frozenset())


def line(text: str, top: float, *, size: float = BODY_SIZE, bold: bool = False) -> Line:
    return Line(text, size, bold, 43.0, 500.0, top, top + size)


def table(rows: list[list[str | None]], top: float = 100.0, bottom: float = 700.0) -> TableBlock:
    return TableBlock(43.0, top, 500.0, bottom, rows)


def build(*pages: list[Line | TableBlock], skip: tuple[str, ...] = ()) -> list[DocumentElement]:
    builder = StructureBuilder(STATS, TextCleaner(PdfOptions()), frozenset(skip))
    for number, items in enumerate(pages, start=1):
        builder.add_page(PageFlow(number, 595.0, 792.0, list(items)))
    _text, elements = builder.finish()
    return elements


def kinds(elements: list[DocumentElement]) -> list[str]:
    return [e.kind.value for e in elements]


def test_headings_are_judged_against_body_size_not_fixed_sizes() -> None:
    elements = build(
        [
            line("Big Heading", 50, size=16),
            line("Body text at the document's own size.", 80),
            line("Short bold label", 110, bold=True),
            line("More body text follows here.", 130),
        ]
    )
    assert kinds(elements) == ["heading", "paragraph", "heading", "paragraph"]
    assert elements[2].section == "Big Heading > Short bold label"  # bold sits below the big one


def test_body_sized_text_is_never_a_heading() -> None:
    elements = build([line(f"Line {i} of a long paragraph", 50 + 14 * i) for i in range(6)])
    assert kinds(elements) == ["paragraph"]
    assert elements[0].text.count("Line") == 6


def test_paragraphs_split_on_extra_vertical_space() -> None:
    elements = build(
        [
            line("First para line one", 50),
            line("first para line two", 64),
            line("Second paragraph", 100),
        ]
    )
    assert [e.text for e in elements] == [
        "First para line one first para line two",
        "Second paragraph",
    ]


def test_unfinished_paragraph_continues_across_pages_but_finished_one_does_not() -> None:
    continued = build([line("An unfinished sentence that", 700)], [line("keeps going here.", 40)])
    assert [e.text for e in continued] == ["An unfinished sentence that keeps going here."]

    separate = build([line("A finished sentence.", 700)], [line("A new paragraph.", 40)])
    assert len(separate) == 2


def test_closing_quote_alone_does_not_end_a_sentence() -> None:
    elements = build([line('which emitted "beeps"', 700)], [line("heard around the globe.", 40)])
    assert len(elements) == 1


def test_first_row_is_the_header_unless_it_is_mostly_numbers() -> None:
    rows: list[list[str | None]] = [["Name", "Qty"], ["Bolt", "10"], ["Nut", "20"]]
    with_header = build([table(rows)])
    assert [e.text for e in with_header] == ["Name: Bolt | Qty: 10", "Name: Nut | Qty: 20"]

    data_only = build([table([["1", "2.5"], ["3", "4.5"]])])
    assert [e.text for e in data_only] == [
        "Column 1: 1 | Column 2: 2.5",
        "Column 1: 3 | Column 2: 4.5",
    ]


def test_table_continues_across_pages_and_repeated_header_is_dropped() -> None:
    header: list[str | None] = ["Name", "Qty"]
    elements = build(
        [table([header, ["Bolt", "10"]])],
        [table([header, ["Nut", "20"]])],  # header repeated on the next page
        [table([["Screw", "30"]])],  # no header repeat
    )
    assert [e.text for e in elements] == [
        "Name: Bolt | Qty: 10",
        "Name: Nut | Qty: 20",
        "Name: Screw | Qty: 30",
    ]


def test_table_after_prose_is_a_new_table_not_a_continuation() -> None:
    a: list[list[str | None]] = [["Name", "Qty"], ["Bolt", "10"]]
    b: list[list[str | None]] = [["Tool", "Cost"], ["Saw", "12"]]
    elements = build([table(a)], [line("Some prose in between.", 40), table(b, top=80)])
    rows = [e.text for e in elements if e.kind is ElementKind.TABLE_ROW]
    assert rows == ["Name: Bolt | Qty: 10", "Tool: Saw | Cost: 12"]


def test_short_line_above_a_table_is_its_caption() -> None:
    rows: list[list[str | None]] = [["Name", "Qty"], ["Bolt", "10"]]
    elements = build([line("Table 1: Hardware stock", 80), table(rows, top=100)])
    assert kinds(elements) == ["table_row"]
    assert elements[0].table == "Table 1: Hardware stock"


def test_sentence_above_a_table_is_prose_not_a_caption() -> None:
    rows: list[list[str | None]] = [["Name", "Qty"], ["Bolt", "10"]]
    elements = build([line("The table below lists the stock.", 80), table(rows, top=100)])
    assert kinds(elements) == ["paragraph", "table_row"]
    assert elements[1].table == ""


def test_single_cell_row_is_prose_and_empty_cells_are_omitted() -> None:
    rows: list[list[str | None]] = [["A", "B"], ["A remark spanning the table", None], ["x", ""]]
    elements = build([table(rows)])
    assert [(e.kind.value, e.text) for e in elements] == [
        ("paragraph", "A remark spanning the table"),
        ("table_row", "A: x"),
    ]


def test_skipped_section_is_dropped_until_a_heading_of_equal_or_higher_level() -> None:
    elements = build(
        [
            line("Intro", 40, size=16),
            line("Kept text.", 70),
            line("References", 100, size=16),
            line("Dropped citation text.", 130),
            line("Afterword", 160, size=16),
            line("Kept again.", 190),
        ],
        skip=("references",),
    )
    assert [e.text for e in elements] == ["Intro", "Kept text.", "Afterword", "Kept again."]


def test_elements_carry_offsets_into_the_joined_text() -> None:
    elements = build([line("Heading", 40, size=16), line("Some body text.", 70)])
    text = "\n".join(e.text for e in elements)
    assert all(text[e.char_start : e.char_end] == e.text for e in elements)


def test_cleaner_removes_characters_the_database_cannot_store() -> None:
    clean = cleaner()
    nul, bell, surrogate = chr(0), chr(7), chr(0xD800)
    assert clean.clean(f"ab{nul}cd{bell}ef{surrogate}") == "abcdef"
    assert clean.removed_characters == 3
    assert clean.clean("kept\tand\nwhitespace") == "kept and whitespace"
    assert clean.removed_characters == 3  # ordinary whitespace is not "invalid"
