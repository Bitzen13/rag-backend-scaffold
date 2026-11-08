"""Table handling shared by every reader that can produce table rows.

A table row becomes one retrievable element only if it carries its column names, so the rules for
deciding what the header row is, and for rendering a row, live here rather than in each reader.
"""

import re
from collections.abc import Sequence

from app.extraction.base import DocumentElement, ElementKind

HEADER_MAX_NUMERIC_FRACTION = 0.5  # a candidate header that is mostly numbers is data, not a header
FIELD_SEPARATOR = " | "

_NUMERIC_CELL = re.compile(r"^[\s\d.,+\-−–<>~≈≳±%$€£()/:]+$")


def is_numeric_cell(cell: str) -> bool:
    """True for a cell that is only a number and its decoration (units, ranges, tolerances)."""
    return bool(_NUMERIC_CELL.match(cell)) and any(ch.isdigit() for ch in cell)


def looks_like_header(cells: Sequence[str]) -> bool:
    """True when a row reads like column names rather than data."""
    filled = [c for c in cells if c]
    if not filled:
        return False
    numeric = sum(is_numeric_cell(c) for c in filled)
    return numeric / len(filled) < HEADER_MAX_NUMERIC_FRACTION


def header_names(cells: Sequence[str]) -> list[str]:
    """Column names for a header row: blanks become "Column N", duplicates get their position."""
    names: list[str] = []
    for position, cell in enumerate(cells, start=1):
        name = cell or f"Column {position}"
        names.append(f"{name} ({position})" if name in names else name)
    return names


def positional_names(count: int) -> list[str]:
    return [f"Column {i}" for i in range(1, count + 1)]


def row_fields(headers: Sequence[str], cells: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Pair column names with a row's values, dropping empty cells.

    A row with a different number of cells than the header falls back to positional names, so a
    ragged table still produces usable rows instead of mismatched labels.
    """
    names = list(headers) if len(headers) == len(cells) else positional_names(len(cells))
    return tuple((name, value) for name, value in zip(names, cells, strict=True) if value)


def render_row(fields: Sequence[tuple[str, str]]) -> str:
    """A table row as self-describing text: "Header: value | Header: value"."""
    return FIELD_SEPARATOR.join(f"{name}: {value}" for name, value in fields)


def table_row_element(
    headers: Sequence[str],
    cells: Sequence[str],
    *,
    table: str = "",
    section: str = "",
    page: int = 0,
    line_start: int = 0,
    line_end: int = 0,
) -> DocumentElement | None:
    """Build a table-row element, or None when the row holds no values worth retrieving."""
    fields = row_fields(headers, cells)
    if not fields:
        return None
    return DocumentElement(
        kind=ElementKind.TABLE_ROW,
        text=render_row(fields),
        page=page,
        section=section,
        table=table,
        fields=fields,
        line_start=line_start,
        line_end=line_end,
    )


class TableReader:
    """Turns a stream of rows into row elements, deciding the header from the first row.

    Readers that see a table as plain rows (CSV, a sheet, an HTML or Markdown table) share this so
    that header detection, ragged rows and header-less tables behave identically everywhere.
    """

    def __init__(self, *, table: str = "", section: str = "", page: int = 0) -> None:
        self._table = table
        self._section = section
        self._page = page
        self._headers: list[str] | None = None

    @property
    def headers(self) -> list[str]:
        return list(self._headers or ())

    def set_headers(self, cells: Sequence[str]) -> None:
        self._headers = header_names(cells)

    def add(self, cells: Sequence[str], *, line: int = 0) -> DocumentElement | None:
        """Feed one row; the first row is taken as the header unless it reads like data."""
        values = [c.strip() for c in cells]
        if not any(values):
            return None
        if self._headers is None:
            if looks_like_header(values):
                self.set_headers(values)
                return None
            self._headers = positional_names(len(values))
        return table_row_element(
            self._headers,
            values,
            table=self._table,
            section=self._section,
            page=self._page,
            line_start=line,
            line_end=line,
        )
