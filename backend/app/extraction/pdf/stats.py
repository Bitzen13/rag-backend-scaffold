"""Whole-document statistics, so structure is judged relative to this document's own typography."""

import re
import statistics
from collections import Counter
from dataclasses import dataclass

from app.extraction.pdf.model import Line, PageFlow
from app.extraction.text import squash

HEADING_SIZE_RATIO = 1.15  # a line this much larger than body text is a heading
MAX_HEADING_WORDS = 25  # longer lines are prose, however large (e.g. a lead paragraph)
FURNITURE_BAND = 0.07  # fraction of page height at the top/bottom where running heads live
FURNITURE_MIN_PAGES = 3
FURNITURE_MIN_PAGE_FRACTION = 0.3  # repeated on this share of pages => header/footer
PITCH_MAX_BODY_MULTIPLE = 2.5  # line-to-line gaps larger than this are not one paragraph's lines
DEFAULT_PITCH_MULTIPLE = 1.3  # line pitch assumed when too few measurements exist

_DIGITS = re.compile(r"\d+")


def normalize(text: str) -> str:
    """Text with numbers masked, so "Page 3" and "Page 4" count as the same running footer."""
    return _DIGITS.sub("#", squash(text).lower())


def round_size(size: float) -> float:
    return round(size * 2) / 2


def in_furniture_band(line: Line, page_height: float) -> bool:
    return (
        line.top < FURNITURE_BAND * page_height or line.bottom > (1 - FURNITURE_BAND) * page_height
    )


@dataclass(frozen=True)
class DocStats:
    body_size: float
    line_pitch: float  # typical distance between consecutive lines of one paragraph
    heading_sizes: tuple[float, ...]  # descending; index + 1 is the heading level
    furniture: frozenset[str]  # normalized text of running headers/footers


def collect_stats(pages: list[PageFlow]) -> DocStats:
    lines = [item for page in pages for item in page.items if isinstance(item, Line)]
    if not lines:
        return DocStats(10.0, 13.0, (), frozenset())

    weights: Counter[float] = Counter()
    for line in lines:
        weights[round(line.size, 1)] += len(line.text)
    body = weights.most_common(1)[0][0]

    heading_sizes = sorted(
        {
            round_size(line.size)
            for line in lines
            if line.size >= body * HEADING_SIZE_RATIO
            and len(line.text.split()) <= MAX_HEADING_WORDS
        },
        reverse=True,
    )
    return DocStats(
        body_size=body,
        line_pitch=_line_pitch(pages, body),
        heading_sizes=tuple(heading_sizes),
        furniture=_furniture(pages),
    )


def _line_pitch(pages: list[PageFlow], body: float) -> float:
    gaps: list[float] = []
    for page in pages:
        previous: Line | None = None
        for item in page.items:
            if not isinstance(item, Line):
                previous = None
                continue
            if (
                previous is not None
                and abs(previous.size - body) <= 0.5
                and abs(item.size - body) <= 0.5
                and abs(previous.x0 - item.x0) <= 3 * body
                and 0 < item.top - previous.top <= PITCH_MAX_BODY_MULTIPLE * body
            ):
                gaps.append(item.top - previous.top)
            previous = item
    return statistics.median(gaps) if len(gaps) >= 5 else body * DEFAULT_PITCH_MULTIPLE


def _furniture(pages: list[PageFlow]) -> frozenset[str]:
    pages_with: Counter[str] = Counter()
    for page in pages:
        keys = {
            normalize(item.text)
            for item in page.items
            if isinstance(item, Line) and in_furniture_band(item, page.height)
        }
        pages_with.update(keys)
    needed = max(FURNITURE_MIN_PAGES, FURNITURE_MIN_PAGE_FRACTION * len(pages))
    return frozenset(key for key, count in pages_with.items() if key and count >= needed)
