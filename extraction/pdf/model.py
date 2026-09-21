"""Plain data shared by the PDF reader's stages."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Line:
    """One line of prose (text outside any table) with the measurements the analysis needs."""

    text: str
    size: float  # dominant font size, in points
    bold: bool
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True)
class TableBlock:
    """A detected table: its position on the page and its raw cell text, row by row."""

    x0: float
    top: float
    x1: float
    bottom: float
    rows: list[list[str | None]]

    @property
    def ncols(self) -> int:
        return max(len(row) for row in self.rows)


@dataclass
class PageFlow:
    """Everything on one page, in reading order: prose lines and tables."""

    number: int
    width: float
    height: float
    items: list[Line | TableBlock] = field(default_factory=list)
    has_images: bool = False  # true with no text at all suggests a scanned page
