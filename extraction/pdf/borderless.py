"""Tables that are aligned but not ruled.

pdfplumber finds tables from their ruling lines, which leaves the very common table that is laid
out with whitespace alone. Its text would otherwise be read as prose, and a row's values would be
separated from their column names.

Detection is deliberately hard to trigger, because a false positive turns real prose into
nonsense rows. A run of lines is a table only when the lines agree: the same number of cells, at
the same left edges, several rows deep, with cells short enough to be values rather than
sentences.
"""

import itertools
import re
import statistics
from collections.abc import Sequence
from typing import Any

from extraction.pdf.model import TableBlock

MIN_ROWS = 3  # fewer aligned lines than this is a coincidence
MIN_COLUMNS = 2
GAP_MIN_SIZES = 1.6  # a gap this many font sizes wide separates two cells
ALIGN_TOLERANCE_SIZES = 0.8  # cell left edges may differ by this much and still be one column
ALIGN_MIN_FRACTION = 0.7  # this share of a run's rows must fill each detected column
COMPLETE_MIN_FRACTION = 0.8  # this share of rows must fill every column: a table is not ragged
CELL_MAX_WORDS = 8  # a "cell" longer than this is a sentence, so the run is prose
CELL_LONG_MAX_FRACTION = 0.2  # tolerated share of long cells in a run
CELL_MEAN_MAX_WORDS = 4.0  # cells hold values; prose laid out in columns holds phrases
PROSE_FLOW_MAX_FRACTION = 0.4  # rows that read on into the next row: wrapped prose, not a table

# A sentence ending inside a cell: a cell holds one value, not a sentence and the next one.
_SENTENCE_BREAK = re.compile(r"[.!?][\s ]+[A-Z(“\"']")
_TERMINAL_PUNCTUATION = (".", "!", "?", ":", ";", ",")

Word = dict[str, Any]
Cell = tuple[float, float, str]  # x0, x1, text


def _word_size(word: Word) -> float:
    return float(word.get("size", 10.0) or 10.0)


def split_cells(words: Sequence[Word]) -> list[Cell]:
    """Split one line of words into cells wherever a wide gap separates them."""
    ordered = sorted(words, key=lambda w: w["x0"])
    if not ordered:
        return []
    cells: list[list[Word]] = [[ordered[0]]]
    for word in ordered[1:]:
        previous = cells[-1][-1]
        size = max(_word_size(previous), _word_size(word))
        if word["x0"] - previous["x1"] > GAP_MIN_SIZES * size:
            cells.append([])
        cells[-1].append(word)
    return [
        (
            float(min(w["x0"] for w in cell)),
            float(max(w["x1"] for w in cell)),
            " ".join(str(w["text"]) for w in cell),
        )
        for cell in cells
        if cell
    ]


def _columns(rows: Sequence[list[Cell]], tolerance: float) -> list[float]:
    """The left edges the rows agree on, each shared by enough rows to be a column."""
    edges = sorted(cell[0] for row in rows for cell in row)
    if not edges:
        return []
    clusters: list[list[float]] = [[edges[0]]]
    for edge in edges[1:]:
        if edge - clusters[-1][-1] <= tolerance:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    needed = ALIGN_MIN_FRACTION * len(rows)
    return [statistics.median(cluster) for cluster in clusters if len(cluster) >= needed]


def _assign(row: list[Cell], columns: list[float], tolerance: float) -> list[str | None] | None:
    """Place a row's cells into the detected columns, or None if a cell fits none of them."""
    placed: list[str | None] = [None] * len(columns)
    for x0, _, text in row:
        distances = [abs(x0 - column) for column in columns]
        best = min(range(len(columns)), key=lambda index: distances[index])
        if distances[best] > tolerance:
            return None
        placed[best] = f"{placed[best]} {text}".strip() if placed[best] else text
    return placed


def _is_tabular(rows: Sequence[list[Cell]]) -> bool:
    """Whether a run of aligned lines reads as a table rather than as prose set in columns.

    The alignment tests alone accept text beside a figure, or a wrapped caption, because those
    happen to line up. What separates a table is what is in the cells: short values, no sentence
    running through a cell, and no row reading on into the next one.
    """
    cells = [cell for row in rows for cell in row]
    if not cells:
        return False
    lengths = [len(text.split()) for _, _, text in cells]
    if sum(length > CELL_MAX_WORDS for length in lengths) > CELL_LONG_MAX_FRACTION * len(cells):
        return False
    if statistics.fmean(lengths) > CELL_MEAN_MAX_WORDS:
        return False
    if any(_SENTENCE_BREAK.search(text) for _, _, text in cells):
        return False
    return _prose_flow(rows) <= PROSE_FLOW_MAX_FRACTION


def _prose_flow(rows: Sequence[list[Cell]]) -> float:
    """The share of adjacent row pairs where the first row reads on into the second."""
    pairs = list(itertools.pairwise(rows))
    if not pairs:
        return 0.0
    flowing = 0
    for upper, lower in pairs:
        if not upper or not lower:
            continue
        ends_open = not upper[-1][2].rstrip().endswith(_TERMINAL_PUNCTUATION)
        continues = lower[0][2][:1].islower()
        flowing += ends_open and continues
    return flowing / len(pairs)


def _is_complete(rows: Sequence[list[Cell]], columns: int) -> bool:
    """A table's rows mostly fill all of its columns; aligned prose fills them at random."""
    full = sum(len(row) == columns for row in rows)
    return full >= COMPLETE_MIN_FRACTION * len(rows)


def find_borderless_tables(lines: Sequence[Sequence[Word]]) -> list[TableBlock]:
    """Find whitespace-aligned tables among a page's lines of words, in reading order.

    `lines` are the page's lines, each a list of word dictionaries, top to bottom. Returns a block
    per detected table; lines not part of one are left for the prose reader.
    """
    candidates = [(index, split_cells(line)) for index, line in enumerate(lines)]
    runs: list[list[int]] = []
    for index, cells in candidates:
        if len(cells) >= MIN_COLUMNS:
            if runs and runs[-1][-1] == index - 1:
                runs[-1].append(index)
            else:
                runs.append([index])

    blocks: list[TableBlock] = []
    for run in runs:
        if len(run) < MIN_ROWS:
            continue
        rows = [candidates[index][1] for index in run]
        sizes = [_word_size(word) for index in run for word in lines[index]]
        tolerance = ALIGN_TOLERANCE_SIZES * (statistics.median(sizes) if sizes else 10.0)
        columns = _columns(rows, tolerance)
        if (
            len(columns) < MIN_COLUMNS
            or not _is_complete(rows, len(columns))
            or not _is_tabular(rows)
        ):
            continue
        assigned = [_assign(row, columns, tolerance) for row in rows]
        if any(row is None for row in assigned):
            continue
        words = [word for index in run for word in lines[index]]
        blocks.append(
            TableBlock(
                x0=float(min(w["x0"] for w in words)),
                top=float(min(w["top"] for w in words)),
                x1=float(max(w["x1"] for w in words)),
                bottom=float(max(w["bottom"] for w in words)),
                rows=[row for row in assigned if row is not None],
                borderless=True,
            )
        )
    return blocks
