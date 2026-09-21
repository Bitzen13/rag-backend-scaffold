"""Turns analysed pages into ordered document elements: headings, paragraphs, and table rows.

Decisions are made from the document's own statistics (see stats.py) and from layout, never from
the wording of particular headings or column names:
- headings are lines set larger than body text, or short bold lines;
- a table's first row is its header unless that row looks like data (mostly numeric);
- a table that begins a page right after a table that reached the bottom of the previous page
  continues it: it reuses the earlier headers, and a repeated header row is dropped;
- the short line just above a table is its caption.
"""

import re
from dataclasses import dataclass, field

from extraction.base import DocumentElement, ElementKind
from extraction.elements import place_elements
from extraction.pdf.clean import TextCleaner
from extraction.pdf.model import Line, PageFlow, TableBlock
from extraction.pdf.stats import (
    HEADING_SIZE_RATIO,
    MAX_HEADING_WORDS,
    DocStats,
    in_furniture_band,
    normalize,
    round_size,
)

PARAGRAPH_BREAK_PITCHES = 1.5  # a gap this many line pitches wide starts a new paragraph
STREAM_MAX_GAP_PITCHES = 3.0  # lines farther apart than this are not the same paragraph
STREAM_X_TOLERANCE_SIZES = 3.0  # line start may shift this many font sizes (indents, hanging)
SIZE_TOLERANCE = 0.6  # points; lines of this size difference can belong to one paragraph
MAX_BOLD_HEADING_WORDS = 12
MIN_BOLD_HEADING_SIZE = 0.9  # bold lines smaller than this share of body text are labels
HEADING_MERGE_GAP = 0.9  # wrapped heading lines are at most this many font sizes apart
CAPTION_MAX_WORDS = 20
CAPTION_MAX_LINES = 2
CAPTION_MAX_GAP_PITCHES = 2.0
CONTINUATION_MIN_BOTTOM = 0.75  # a table reaching this far down its page may continue on the next
HEADER_MAX_NUMERIC_FRACTION = 0.5  # a first row that is mostly numbers is data, not a header

_SENTENCE_END = (".", "!", "?", ":", ";", "…")
_CLOSERS = " \"'”’)]"  # closing quotes/brackets can follow the final punctuation
_BULLET = re.compile(r"^(?:[•◦▪‣∙*\-–]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s")
_NUMERIC_CELL = re.compile(r"^[\s\d.,+\-−–<>~≈≳±%$€£()/:]+$")


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_SENTENCE_END)


def _is_numeric(cell: str) -> bool:
    return bool(_NUMERIC_CELL.match(cell)) and any(ch.isdigit() for ch in cell)


@dataclass
class _Stream:
    """A run of consecutive lines forming one paragraph."""

    lines: list[Line]
    texts: list[str]
    first_page: int
    page: int

    @property
    def last(self) -> Line:
        return self.lines[-1]

    def text(self) -> str:
        return " ".join(self.texts)


def _overlaps(stream: _Stream, line: Line) -> bool:
    left = min(ln.x0 for ln in stream.lines)
    right = max(ln.x1 for ln in stream.lines)
    return min(right, line.x1) > max(left, line.x0)


@dataclass
class _PendingHeading:
    level: int
    parts: list[str]
    last: Line
    page: int


@dataclass
class _LastTable:
    page: int
    ncols: int
    reaches_bottom: bool


@dataclass
class StructureBuilder:
    stats: DocStats
    cleaner: TextCleaner
    skip_sections: frozenset[str]
    elements: list[DocumentElement] = field(default_factory=list)
    _page: int = 0
    _height: float = 0.0
    _stack: list[tuple[int, str]] = field(default_factory=list)
    _skip_level: int | None = None
    _streams: list[_Stream] = field(default_factory=list)
    _pending: _PendingHeading | None = None
    _table_title: str = ""
    _headers: list[str] | None = None
    _header_cells: list[str] | None = None
    _last_table: _LastTable | None = None

    # ---- pages ----------------------------------------------------------------------------

    def add_page(self, page: PageFlow) -> None:
        self._page = page.number
        self._height = page.height
        items = [
            item
            for item in page.items
            if not (
                isinstance(item, Line)
                and in_furniture_band(item, page.height)
                and normalize(item.text) in self.stats.furniture
            )
        ]
        for index, item in enumerate(items):
            if isinstance(item, TableBlock):
                self._table(item, first_on_page=index == 0)
            else:
                self._line(item)
        self._flush_heading()
        self._flush_streams(keep_unfinished=True)

    def finish(self) -> tuple[str, list[DocumentElement]]:
        """Flush pending text and return the document text with each element's offsets in it."""
        self._flush_heading()
        self._flush_streams()
        text, placed = place_elements(self.elements)
        return text, list(placed)

    # ---- prose ----------------------------------------------------------------------------

    def _line(self, line: Line) -> None:
        text = self.cleaner.clean(line.text)
        if not text:
            return
        self._last_table = None  # any prose between two tables means the second is a new table
        if self._is_heading(line, text):
            self._heading_line(line, text)
            return
        self._flush_heading()
        self._place(line, text)

    def _is_heading(self, line: Line, text: str) -> bool:
        words = len(text.split())
        if line.size >= self.stats.body_size * HEADING_SIZE_RATIO and words <= MAX_HEADING_WORDS:
            return True
        return (
            line.bold
            and line.size >= self.stats.body_size * MIN_BOLD_HEADING_SIZE
            and words <= MAX_BOLD_HEADING_WORDS
            and not _ends_sentence(text)
        )

    def _level(self, line: Line) -> int:
        sizes = self.stats.heading_sizes
        size = round_size(line.size)
        return sizes.index(size) + 1 if size in sizes else len(sizes) + 1

    def _heading_line(self, line: Line, text: str) -> None:
        self._flush_streams(beside=line)
        level = self._level(line)
        pending = self._pending
        if (
            pending is not None
            and pending.level == level
            and pending.page == self._page
            and line.top - pending.last.bottom < HEADING_MERGE_GAP * line.size
        ):
            pending.parts.append(text)
            pending.last = line
            return
        self._flush_heading()
        self._pending = _PendingHeading(level, [text], line, self._page)

    def _flush_heading(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        title = " ".join(pending.parts)
        while self._stack and self._stack[-1][0] >= pending.level:
            self._stack.pop()
        self._stack.append((pending.level, title))
        if self._skip_level is not None and pending.level <= self._skip_level:
            self._skip_level = None
        if self._skip_level is None and title.lower() in self.skip_sections:
            self._skip_level = pending.level
        if self._skip_level is None:
            self.elements.append(
                DocumentElement(ElementKind.HEADING, title, pending.page, self._section())
            )

    def _section(self) -> str:
        return " > ".join(title for _, title in self._stack)

    def _place(self, line: Line, text: str) -> None:
        """Add a line to the paragraph it continues, or start a new one."""
        pitch = self.stats.line_pitch
        x_tolerance = STREAM_X_TOLERANCE_SIZES * self.stats.body_size
        best: _Stream | None = None
        best_gap = float("inf")
        for stream in self._streams:
            last = stream.last
            gap = line.top - last.top
            if (
                stream.page == self._page
                and abs(last.size - line.size) <= SIZE_TOLERANCE
                and abs(last.x0 - line.x0) <= x_tolerance
                and -1 <= gap <= STREAM_MAX_GAP_PITCHES * pitch
                and gap < best_gap
            ):
                best, best_gap = stream, gap
        if best is not None:
            if _BULLET.match(text) or best_gap > PARAGRAPH_BREAK_PITCHES * pitch:
                self._streams.remove(best)
                self._emit_paragraph(best)
                self._streams.append(_Stream([line], [text], self._page, self._page))
            else:
                best.lines.append(line)
                best.texts.append(text)
            return

        # No paragraph continues here on this page. Text that wrapped (to a new column or page)
        # continues an unfinished sentence.
        wrapped = [
            s
            for s in self._streams
            if abs(s.last.size - line.size) <= SIZE_TOLERANCE
            and not _ends_sentence(s.texts[-1])
            and (s.page != self._page or line.top < s.last.top - 1)
        ]
        if wrapped and not _BULLET.match(text):
            stream = max(wrapped, key=lambda s: len(s.text()))
            stream.lines.append(line)
            stream.texts.append(text)
            stream.page = self._page
            return
        self._streams.append(_Stream([line], [text], self._page, self._page))

    def _emit_paragraph(self, stream: _Stream) -> None:
        text = stream.text()
        if text and self._skip_level is None:
            self.elements.append(
                DocumentElement(ElementKind.PARAGRAPH, text, stream.first_page, self._section())
            )

    def _flush_streams(self, *, keep_unfinished: bool = False, beside: Line | None = None) -> None:
        """Emit open paragraphs in reading order. At a page end, the body paragraph with an
        unfinished sentence that runs lowest on the page stays open: it may continue next page.
        With `beside`, only paragraphs that overlap that line horizontally are closed; text set
        alongside it (a figure caption in the margin) keeps flowing."""
        keep: _Stream | None = None
        if keep_unfinished:
            body = self.stats.body_size
            open_ended = [
                s
                for s in self._streams
                if not _ends_sentence(s.texts[-1]) and abs(s.last.size - body) <= SIZE_TOLERANCE
            ]
            # The body paragraph that runs lowest on the page is the one that carries over.
            keep = max(open_ended, key=lambda s: s.last.bottom, default=None)
        streams, self._streams = self._streams, []
        for stream in sorted(streams, key=lambda s: (s.lines[0].top, s.lines[0].x0)):
            if stream is keep or (beside is not None and not _overlaps(stream, beside)):
                self._streams.append(stream)
            else:
                self._emit_paragraph(stream)

    # ---- tables ---------------------------------------------------------------------------

    def _table(self, block: TableBlock, *, first_on_page: bool) -> None:
        self._flush_heading()
        last = self._last_table
        continuation = (
            first_on_page
            and last is not None
            and last.page == self._page - 1
            and last.ncols == block.ncols
            and last.reaches_bottom
        )
        caption = "" if continuation else self._take_caption(block)
        self._flush_streams()
        if not continuation:
            self._table_title = caption
            self._headers = None
            self._header_cells = None

        expect_header = not continuation
        check_repeat = continuation and self._header_cells is not None
        for raw in block.rows:
            if not any(c and c.strip() for c in raw):
                continue
            if raw[0] and all(cell is None for cell in raw[1:]):
                # A single-cell row (a remark spanning the table) is prose, not a data row.
                self._add_prose(self.cleaner.clean(raw[0]))
                continue
            cells = [self.cleaner.clean(cell) for cell in raw]
            if expect_header:
                expect_header = False
                if self._looks_like_header(cells):
                    self._set_headers(cells)
                    continue
            elif check_repeat:
                check_repeat = False
                if cells == self._header_cells:
                    continue  # the header repeated at the top of a continuation page
            self._add_row(cells)
        self._last_table = _LastTable(
            self._page, block.ncols, block.bottom >= CONTINUATION_MIN_BOTTOM * self._height
        )

    def _take_caption(self, block: TableBlock) -> str:
        """The short line directly above a table, removed from the prose flow."""
        pitch = self.stats.line_pitch
        best: _Stream | None = None
        for stream in self._streams:
            last = stream.last
            if len(stream.lines) > CAPTION_MAX_LINES or last.bottom > block.top + 1:
                continue
            if block.top - last.bottom > CAPTION_MAX_GAP_PITCHES * pitch:
                continue
            text = stream.text()
            if len(text.split()) > CAPTION_MAX_WORDS or _ends_sentence(text):
                continue
            if min(last.x1, block.x1) <= max(stream.lines[0].x0, block.x0):
                continue  # not above the table
            if best is None or last.bottom > best.last.bottom:
                best = stream
        if best is None:
            return ""
        self._streams.remove(best)
        return best.text()

    @staticmethod
    def _looks_like_header(cells: list[str]) -> bool:
        filled = [c for c in cells if c]
        if not filled:
            return False
        numeric = sum(_is_numeric(c) for c in filled)
        return numeric / len(filled) < HEADER_MAX_NUMERIC_FRACTION

    def _set_headers(self, cells: list[str]) -> None:
        names: list[str] = []
        for position, cell in enumerate(cells, start=1):
            name = cell or f"Column {position}"
            names.append(f"{name} ({position})" if name in names else name)
        self._headers = names
        self._header_cells = cells

    def _add_row(self, cells: list[str]) -> None:
        headers = self._headers
        if headers is None or len(headers) != len(cells):
            headers = [f"Column {i}" for i in range(1, len(cells) + 1)]
        fields = tuple((h, v) for h, v in zip(headers, cells, strict=True) if v)
        if not fields or self._skip_level is not None:
            return
        self.elements.append(
            DocumentElement(
                kind=ElementKind.TABLE_ROW,
                text=" | ".join(f"{h}: {v}" for h, v in fields),
                page=self._page,
                section=self._section(),
                table=self._table_title,
                fields=fields,
            )
        )

    def _add_prose(self, text: str) -> None:
        if text and self._skip_level is None:
            self.elements.append(
                DocumentElement(ElementKind.PARAGRAPH, text, self._page, self._section())
            )
