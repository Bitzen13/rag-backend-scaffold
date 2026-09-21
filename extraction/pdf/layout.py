"""Page analysis: find tables, then put the remaining prose lines and the tables in reading order.

Nothing here assumes a particular document. Tables are those pdfplumber finds from ruling lines
(borderless tables are not detected: their text is read as prose). Columns are found from
vertical gaps in the words themselves.
"""

import statistics
from collections import Counter
from typing import Any

from pdfplumber.page import Page
from pdfplumber.utils.clustering import cluster_objects

from extraction.pdf.model import Line, PageFlow, TableBlock

LINE_TOLERANCE = 3.0  # points: words whose tops differ by less share a line
CELL_LINE_TOLERANCE = 4.0  # merges stacked super/subscripts inside one table cell into one line
BOLD_LINE_FRACTION = 0.6  # a line is bold when this share of its characters is bold

MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2

MIN_WORDS_FOR_COLUMNS = 40
GUTTER_MIN_FONT_MULTIPLE = 1.3  # a column gap is at least this many font sizes wide
GUTTER_EMPTY_FRACTION = 0.05  # occupancy this low (relative to the peak) counts as empty
GUTTER_MIN_SIDE_FRACTION = 0.2  # each side of a gutter must hold this share of the words
# Text on one baseline is split into independent segments (e.g. body text beside a figure caption)
# when the gap is very wide, or wide and the font or size changes.
SEGMENT_HARD_GAP_SIZES = 4.0
SEGMENT_STYLE_GAP_SIZES = 1.0
SEGMENT_SIZE_CHANGE = 0.6  # points
PLACEMENT_MIN_OVERLAP = 0.5  # a table sits before the first line it overlaps this much

Rows = list[list[str | None]]


def _is_ruled_table(rows: Rows) -> bool:
    return len(rows) >= MIN_TABLE_ROWS and max(len(row) for row in rows) >= MIN_TABLE_COLS


def _find_ruled_tables(page: Page) -> list[TableBlock]:
    """Tables drawn with ruling lines, which pdfplumber finds reliably."""
    blocks: list[TableBlock] = []
    for table in page.find_tables():
        rows = table.extract(y_tolerance=CELL_LINE_TOLERANCE)
        if _is_ruled_table(rows):
            x0, top, x1, bottom = table.bbox
            blocks.append(TableBlock(x0, top, x1, bottom, rows))
    return blocks


def _make_line(words: list[dict[str, Any]]) -> Line:
    words = sorted(words, key=lambda w: w["x0"])
    sizes: Counter[float] = Counter()
    bold_chars = total_chars = 0
    for word in words:
        chars = len(word["text"])
        sizes[round(float(word["size"]), 1)] += chars
        total_chars += chars
        if "bold" in str(word["fontname"]).lower():
            bold_chars += chars
    return Line(
        text=" ".join(w["text"] for w in words),
        size=sizes.most_common(1)[0][0],
        bold=bold_chars >= BOLD_LINE_FRACTION * total_chars,
        x0=min(w["x0"] for w in words),
        x1=max(w["x1"] for w in words),
        top=min(w["top"] for w in words),
        bottom=max(w["bottom"] for w in words),
    )


def _segments(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split one baseline of words wherever a wide gap separates independent text."""
    ordered = sorted(words, key=lambda w: w["x0"])
    segments = [[ordered[0]]]
    for word in ordered[1:]:
        previous = segments[-1][-1]
        size = max(float(previous["size"]), float(word["size"]))
        gap = word["x0"] - previous["x1"]
        restyled = (
            word["fontname"] != previous["fontname"]
            or abs(float(word["size"]) - float(previous["size"])) > SEGMENT_SIZE_CHANGE
        )
        if gap > SEGMENT_HARD_GAP_SIZES * size or (
            restyled and gap > SEGMENT_STYLE_GAP_SIZES * size
        ):
            segments.append([])
        segments[-1].append(word)
    return segments


def _find_gutters(words: list[dict[str, Any]], page_width: float) -> list[tuple[float, float]]:
    """Vertical bands with no text between substantial blocks of text: column gaps."""
    if len(words) < MIN_WORDS_FOR_COLUMNS:
        return []
    min_gap = GUTTER_MIN_FONT_MULTIPLE * statistics.median(float(w["size"]) for w in words)
    occupancy = [0] * (int(page_width) + 2)
    for word in words:
        for x in range(int(word["x0"]), min(int(word["x1"]) + 1, len(occupancy))):
            occupancy[x] += 1
    limit = max(1.0, GUTTER_EMPTY_FRACTION * max(occupancy))
    centers = [(w["x0"] + w["x1"]) / 2 for w in words]
    gutters: list[tuple[float, float]] = []
    start: int | None = None
    for x, count in enumerate([*occupancy, 10**9]):
        if count <= limit:
            start = x if start is None else start
            continue
        if start is not None and x - start >= min_gap:
            left = sum(1 for c in centers if c < start)
            right = sum(1 for c in centers if c >= x)
            need = GUTTER_MIN_SIDE_FRACTION * len(centers)
            if left >= need and right >= need:
                gutters.append((float(start), float(x)))
        start = None
    return gutters


def _reading_order(words: list[dict[str, Any]], gutters: list[tuple[float, float]]) -> list[Line]:
    """Lines in reading order: full-width lines split the page into bands, and within a band each
    column is read top to bottom before the next."""
    raw_lines = cluster_objects(words, lambda w: w["top"], LINE_TOLERANCE)
    raw_lines.sort(key=lambda ws: min(w["top"] for w in ws))
    ordered: list[Line] = []
    band: dict[int, list[Line]] = {}

    def flush() -> None:
        for column in sorted(band):
            ordered.extend(sorted(band[column], key=lambda line: line.top))
        band.clear()

    segments = [segment for line in raw_lines for segment in _segments(line)]
    for line_words in segments:
        if any(w["x0"] < end and w["x1"] > start for w in line_words for start, end in gutters):
            flush()
            ordered.append(_make_line(line_words))
            continue
        by_column: dict[int, list[dict[str, Any]]] = {}
        for word in line_words:
            center = (word["x0"] + word["x1"]) / 2
            column = sum(1 for _, end in gutters if center >= end)
            by_column.setdefault(column, []).append(word)
        for column, column_words in by_column.items():
            band.setdefault(column, []).append(_make_line(column_words))
    flush()
    return ordered


def _overlap_fraction(line: Line, table: TableBlock) -> float:
    shared = min(line.x1, table.x1) - max(line.x0, table.x0)
    return max(0.0, shared) / max(line.x1 - line.x0, 1.0)


def _place_tables(lines: list[Line], tables: list[TableBlock]) -> list[Line | TableBlock]:
    items: list[Line | TableBlock] = list(lines)
    for table in tables:
        index = len(items)
        for i, item in enumerate(items):
            if (
                isinstance(item, Line)
                and item.top >= table.top - 1
                and _overlap_fraction(item, table) >= PLACEMENT_MIN_OVERLAP
            ):
                index = i
                break
        items.insert(index, table)
    return items


def analyze_page(page: Page, number: int) -> PageFlow:
    tables = sorted(_find_ruled_tables(page), key=lambda t: (t.top, t.x0))
    prose = page
    for table in tables:
        prose = prose.outside_bbox((table.x0, table.top, table.x1, table.bottom))
    words = prose.extract_words(
        x_tolerance=3, y_tolerance=LINE_TOLERANCE, extra_attrs=["size", "fontname"]
    )
    lines = _reading_order(words, _find_gutters(words, float(page.width))) if words else []
    return PageFlow(
        number=number,
        width=float(page.width),
        height=float(page.height),
        items=_place_tables(lines, tables),
        has_images=bool(page.images),
    )
