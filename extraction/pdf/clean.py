"""Text cleanup, driven by patterns from config rather than assumptions about any one document."""

import re
from dataclasses import dataclass, field

from extraction.text import squash, strip_invalid_characters

_DANGLING_OPEN_PAREN = re.compile(r"\s*\(\s*$")
_STRAY_PUNCTUATION = frozenset({"<", ">", "~", "-", "–", "—", "|"})


@dataclass(frozen=True)
class OcrSettings:
    """Optional OCR for scanned pages (see extraction/pdf/ocr.py). Off by default."""

    enabled: bool = False
    language: str = "eng"
    min_characters: int = 20  # a page with fewer characters than this looks scanned
    resolution: int = 300


@dataclass(frozen=True)
class PdfOptions:
    """Per-corpus cleanup rules for extracted PDF text (regular expressions).

    strip_patterns: matches are removed wherever they appear (e.g. citation markers "[12]").
    truncate_patterns: text from the first match onward is dropped (e.g. an unrendered template
        marker and whatever follows it).
    skip_sections: headings (case-insensitive) whose section is left out entirely, up to the next
        heading of the same or a higher level (e.g. "References").
    borderless_tables: also detect tables laid out with whitespace rather than ruling lines.
    ocr: read pages that hold an image of text instead of text.
    """

    strip_patterns: tuple[str, ...] = ()
    truncate_patterns: tuple[str, ...] = ()
    skip_sections: tuple[str, ...] = ()
    borderless_tables: bool = True
    ocr: OcrSettings = field(default_factory=OcrSettings)


class TextCleaner:
    def __init__(self, options: PdfOptions) -> None:
        self._strip = [re.compile(p) for p in options.strip_patterns]
        self._truncate = [re.compile(p) for p in options.truncate_patterns]
        self.removed_characters = 0  # unstorable characters (e.g. NUL) dropped so far

    def clean(self, raw: str | None) -> str:
        """Clean a line, cell or header; returns "" when nothing usable is left."""
        if not raw:
            return ""
        text, removed = strip_invalid_characters(raw)
        self.removed_characters += removed
        text = squash(text)
        for pattern in self._truncate:
            match = pattern.search(text)
            if match:
                text = text[: match.start()]
        for pattern in self._strip:
            text = pattern.sub("", text)
        text = _DANGLING_OPEN_PAREN.sub("", squash(text))
        return "" if text in _STRAY_PUNCTUATION else text
