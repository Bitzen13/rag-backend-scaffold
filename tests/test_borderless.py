"""Whitespace-aligned table detection.

The risk here is asymmetric: missing a table loses a little structure, while a false positive
turns prose into nonsense rows. So there are as many tests for what must NOT be detected as for
what must.
"""

from typing import Any

from extraction.pdf.borderless import find_borderless_tables, split_cells

FONT = 10.0


def word(text: str, x0: float, top: float, size: float = FONT) -> dict[str, Any]:
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + len(text) * size * 0.5,
        "top": top,
        "bottom": top + size,
        "size": size,
        "fontname": "Serif",
    }


def line(cells: list[tuple[str, float]], top: float) -> list[dict[str, Any]]:
    """One baseline of words, with each cell's text starting at the given x position."""
    words: list[dict[str, Any]] = []
    for text, x in cells:
        cursor = x
        for part in text.split():
            words.append(word(part, cursor, top))
            cursor = words[-1]["x1"] + 3
    return words


def table_lines() -> list[list[dict[str, Any]]]:
    return [
        line([("Engine", 60), ("Thrust", 200), ("Year", 320)], 100),
        line([("Merlin 1D", 60), ("845", 200), ("2013", 320)], 115),
        line([("RS-25", 60), ("1860", 200), ("1981", 320)], 130),
        line([("Raptor 2", 60), ("2256", 200), ("2023", 320)], 145),
    ]


def test_an_aligned_table_is_found_with_its_rows_and_columns() -> None:
    (block,) = find_borderless_tables(table_lines())
    assert block.borderless
    assert block.rows == [
        ["Engine", "Thrust", "Year"],
        ["Merlin 1D", "845", "2013"],
        ["RS-25", "1860", "1981"],
        ["Raptor 2", "2256", "2023"],
    ]
    assert block.ncols == 3


def test_the_block_covers_the_area_its_rows_occupy() -> None:
    (block,) = find_borderless_tables(table_lines())
    assert block.x0 == 60
    assert block.top == 100
    assert block.bottom == 155


def test_prose_set_in_columns_beside_a_figure_is_not_a_table() -> None:
    """Wrapped caption text lines up by accident; its cells read on into the next row."""
    lines = [
        line(
            [
                ("space. Matter that remained following:", 60),
                ("the initial expansion has since undergone", 300),
            ],
            100,
        ),
        line(
            [
                ("gravitational collapse to create stars,", 60),
                ("the left, the dramatic expansion of", 300),
            ],
            115,
        ),
        line(
            [
                ("galaxies and other astronomical objects,", 60),
                ("inflation; at the center, the:", 300),
            ],
            130,
        ),
    ]
    assert find_borderless_tables(lines) == []


def test_two_aligned_lines_are_not_enough() -> None:
    assert find_borderless_tables(table_lines()[:2]) == []


def test_a_single_column_is_not_a_table() -> None:
    lines = [line([(f"value {n}", 60)], 100 + n * 15) for n in range(4)]
    assert find_borderless_tables(lines) == []


def test_ragged_rows_are_not_a_table() -> None:
    lines = [
        line([("Engine", 60), ("Thrust", 200), ("Year", 320)], 100),
        line([("Merlin", 60)], 115),
        line([("RS-25", 60), ("1860", 200)], 130),
        line([("Raptor", 60)], 145),
    ]
    assert find_borderless_tables(lines) == []


def test_a_cell_holding_two_sentences_rules_out_a_table() -> None:
    lines = [
        line([("One thing. Another thing.", 60), ("value", 300)], 100 + n * 15) for n in range(4)
    ]
    assert find_borderless_tables(lines) == []


def test_ordinary_flowing_paragraph_lines_are_not_a_table() -> None:
    lines = [
        line([(f"this is line {n} of a paragraph that simply wraps", 60)], 100 + n * 15)
        for n in range(6)
    ]
    assert find_borderless_tables(lines) == []


def test_cells_are_split_on_wide_gaps_only() -> None:
    cells = split_cells(line([("two words", 60), ("second cell", 300)], 100))
    assert [text for _, _, text in cells] == ["two words", "second cell"]


def test_no_lines_means_no_tables() -> None:
    assert find_borderless_tables([]) == []
    assert find_borderless_tables([[]]) == []
