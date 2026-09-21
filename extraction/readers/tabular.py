"""Tabular readers: CSV, TSV and Excel workbooks.

A spreadsheet's rows are the retrievable unit, so each row becomes one element carrying its
column names. Two things make this work on real files rather than tidy ones: the delimiter is
sniffed rather than assumed, and the header row is found rather than assumed to be the first row,
since exports routinely start with a title line or a blank row.
"""

import csv
import io
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from db.models import JsonObject
from extraction.elements import ElementList
from extraction.readers.base import ReaderError, ReadResult, require
from extraction.tables import looks_like_header, table_row_element
from extraction.text import decode_bytes, squash

logger = logging.getLogger(__name__)

# How many rows to look at when deciding which one is the header.
HEADER_SEARCH_ROWS = 5
SNIFF_BYTES = 8192
DELIMITERS = ",;\t|"
# A single-cell row above the table is its title, not data.
TITLE_MAX_WORDS = 20

Row = list[str]


def sniff_delimiter(sample: str, default: str = ",") -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        # Fall back to whichever candidate appears most consistently on the first lines.
        lines = [line for line in sample.splitlines()[:10] if line.strip()]
        if not lines:
            return default
        counts = {d: min(line.count(d) for line in lines) for d in DELIMITERS}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else default


def table_width(rows: list[Row]) -> int:
    """How many columns the table really has: the width most of its rows agree on."""
    filled = [row for row in rows if any(cell.strip() for cell in row)]
    if not filled:
        return 0
    return Counter(len(row) for row in filled).most_common(1)[0][0]


def find_header(rows: list[Row]) -> tuple[str, int, Row | None]:
    """Return (table title, index of the first data row, header cells if any).

    Exports rarely start with the header: there is often a blank row or a title line above it, so
    those are skipped, and the first row of the table's own width decides. A file whose first
    substantial row is already data keeps that row and gets positional column names.
    """
    width = table_width(rows[: HEADER_SEARCH_ROWS * 10])
    title = ""
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        values = [cell.strip() for cell in row]
        if not any(values):
            continue
        present = [cell for cell in values if cell]
        if len(present) == 1 and width > 1 and len(present[0].split()) <= TITLE_MAX_WORDS:
            title = title or present[0]  # a lone cell above the table is its title
            continue
        if looks_like_header(values):
            return title, index + 1, values
        return title, index, None
    return title, 0, None


def _rows_to_elements(
    rows: list[Row],
    elements: ElementList,
    *,
    table: str,
    section: str = "",
    page: int = 0,
    line_offset: int = 0,
) -> int:
    """Turn rows into table-row elements; returns how many rows produced one."""
    title, first_data, header = find_header(rows)
    headers = header if header is not None else []
    name = table or title
    if title and table:
        name = f"{table}: {title}"
    written = 0
    for offset, row in enumerate(rows[first_data:], start=first_data):
        values = [cell.strip() for cell in row]
        if not any(values):
            continue
        columns = headers or [f"Column {i}" for i in range(1, len(values) + 1)]
        line = line_offset + offset + 1
        element = table_row_element(
            columns, values, table=name, section=section, page=page, line_start=line, line_end=line
        )
        if element is not None:
            elements.add(element)
            written += 1
    return written


class CsvReader:
    """Delimited text: comma, semicolon, tab or pipe separated."""

    @property
    def name(self) -> str:
        return "csv"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".csv", ".tsv", ".psv")

    def read(self, path: Path) -> ReadResult:
        text, encoding = decode_bytes(path.read_bytes())
        delimiter = "\t" if path.suffix.lower() == ".tsv" else sniff_delimiter(text[:SNIFF_BYTES])
        try:
            rows = [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
        except csv.Error as exc:
            raise ReaderError(f"could not parse {path.name} as delimited text: {exc}") from exc
        elements = ElementList()
        written = _rows_to_elements(rows, elements, table=path.stem.replace("_", " "))
        document_text, placed = elements.build()
        metadata: JsonObject = {
            "encoding": encoding,
            "delimiter": delimiter,
            "rows": written,
        }
        return ReadResult(document_text, placed, metadata)


class ExcelReader:
    """Excel workbooks, one table per worksheet. Formulas are read as their last cached value."""

    @property
    def name(self) -> str:
        return "excel"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".xlsx", ".xlsm", ".xltx")

    def read(self, path: Path) -> ReadResult:
        openpyxl = require("openpyxl", "openpyxl")
        try:
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # openpyxl raises a variety of types on a damaged file
            raise ReaderError(f"could not open {path.name} as a workbook: {exc}") from exc
        elements = ElementList()
        sheets: list[str] = []
        try:
            for number, sheet in enumerate(workbook.worksheets, start=1):
                rows = [
                    [_cell_text(value) for value in row]
                    for row in sheet.iter_rows(values_only=True)
                ]
                written = _rows_to_elements(
                    rows, elements, table=str(sheet.title), section=str(sheet.title), page=number
                )
                if written:
                    sheets.append(str(sheet.title))
        finally:
            workbook.close()
        document_text, placed = elements.build()
        return ReadResult(document_text, placed, {"sheets": sheets})


def _cell_text(value: Any) -> str:
    """A cell as text, without inventing a format for dates or numbers."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return squash(str(value))
