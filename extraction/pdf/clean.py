"""Text cleanup, driven by patterns from config rather than assumptions about any one document."""

import re
from dataclasses import dataclass

from extraction.text import strip_invalid_characters

_SPACES = re.compile(r"\s+")
_DANGLING_OPEN_PAREN = re.compile(r"\s*\(\s*$")
_STRAY_PUNCTUATION = frozenset({"<", ">", "~", "-", "–", "—", "|"})


def squash(text: str) -> str:
    """Collapse all whitespace runs (including newlines) to single spaces."""
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True)
class PdfOptions:
    """Per-corpus cleanup rules for extracted PDF text (regular expressions).

    strip_patterns: matches are removed wherever they appear (e.g. citation markers "[12]").
    truncate_patterns: text from the first match onward is dropped (e.g. an unrendered template
        marker and whatever follows it).
    skip_sections: headings (case-insensitive) whose section is left out entirely, up to the next
        heading of the same or a higher level (e.g. "References").
    """

    strip_patterns: tuple[str, ...] = ()
    truncate_patterns: tuple[str, ...] = ()
    skip_sections: tuple[str, ...] = ()


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
