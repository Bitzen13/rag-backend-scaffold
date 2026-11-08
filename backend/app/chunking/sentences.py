"""Sentence boundaries, without a language model and without assuming English.

Splitting long prose at sentence boundaries keeps a chunk readable and stops a retrieved passage
from starting or ending mid-thought. The rules below are deliberately script-agnostic:

- terminators include the Latin ones and those of CJK, Devanagari, Arabic, Urdu and Ethiopic;
- scripts that put no space after the terminator (CJK) break immediately, while Latin text must
  have whitespace after the terminator, which is also what keeps "3.14" in one piece;
- a lowercase Latin letter after the break means the sentence continues ("e.g. like this"), and
  abbreviations and initials are checked against the word before the terminator;
- text with no terminator at all (Thai, an unpunctuated wall of text) yields a single span, and
  the caller falls back to word windows.
"""

import re

# Terminators that require whitespace (or the end of the text) after them.
_SPACED_TERMINATORS = frozenset(".!?…‼⁇⁈⁉")
# Terminators of scripts that do not put a space after the sentence.
_TIGHT_TERMINATORS = frozenset("。！？｡।॥۔؟።")
_TERMINATORS = _SPACED_TERMINATORS | _TIGHT_TERMINATORS
# Characters that may trail a terminator and still belong to the sentence that ends.
_CLOSERS = frozenset("\"'”’»)]}〉》」』)】\u00a0")

# Words that end in a full stop without ending a sentence. Lower-cased, including the stop.
_ABBREVIATIONS = frozenset(
    {
        # titles and names
        "mr.",
        "mrs.",
        "ms.",
        "dr.",
        "prof.",
        "rev.",
        "hon.",
        "st.",
        "sr.",
        "jr.",
        "messrs.",
        # references and measures
        "fig.",
        "figs.",
        "eq.",
        "ref.",
        "refs.",
        "no.",
        "nos.",
        "vol.",
        "vols.",
        "pp.",
        "ed.",
        "eds.",
        "ch.",
        "sec.",
        "al.",
        "approx.",
        "est.",
        "min.",
        "max.",
        "avg.",
        "cf.",
        # latin and common shorthand
        "e.g.",
        "i.e.",
        "etc.",
        "viz.",
        "vs.",
        "ca.",
        "cca.",
        "ibid.",
        "op.",
        "cit.",
        # places and organisations
        "u.s.",
        "u.k.",
        "u.n.",
        "e.u.",
        "inc.",
        "ltd.",
        "co.",
        "corp.",
        "dept.",
        "univ.",
        # german, french, spanish, italian everyday abbreviations
        "bzw.",
        "z.b.",
        "usw.",
        "ggf.",
        "evtl.",
        "nr.",
        "abb.",
        "vgl.",
        "hrsg.",
        "p.ex.",
        "env.",
        "cie.",
        "art.",
        "pág.",
        "núm.",
        "sig.",
    }
)
_MAX_ABBREVIATION_LENGTH = max(len(a) for a in _ABBREVIATIONS)

_WORD_TAIL = re.compile(r"[^\s]+$")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_WORD = re.compile(r"\S+")


def _is_initial(word: str) -> bool:
    """True for "J." or "J.R.R." - initials, not the end of a sentence."""
    letters = [part for part in word.split(".") if part]
    return bool(letters) and all(len(part) == 1 and part.isalpha() for part in letters)


def _continues(text: str, after: int) -> bool:
    """True when the text after a candidate break clearly continues the same sentence."""
    rest = text[after:].lstrip()
    if not rest:
        return False
    first = rest[0]
    return first.islower() and first.isascii()


def _ends_abbreviation(text: str, terminator: int) -> bool:
    start = max(0, terminator - _MAX_ABBREVIATION_LENGTH)
    match = _WORD_TAIL.search(text[start : terminator + 1])
    if match is None:
        return False
    word = match.group().lower()
    if word in _ABBREVIATIONS or _is_initial(word):
        return True
    # A single capital letter followed by a stop is an initial even mid-word ("VY Canis A. B.").
    return len(word) == 2 and word[0].isalpha()


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans over `text`, trimmed of surrounding whitespace, in order.

    Always returns at least one span for non-empty text, so callers never have to special-case
    text this cannot segment.
    """
    boundaries: list[int] = []
    for match in _BLANK_LINE.finditer(text):
        boundaries.append(match.start())

    position = 0
    length = len(text)
    while position < length:
        char = text[position]
        if char not in _TERMINATORS:
            position += 1
            continue
        end = position + 1
        while end < length and (text[end] in _TERMINATORS or text[end] in _CLOSERS):
            end += 1
        # A tight terminator ends the sentence on its own; a spaced one only when what follows
        # does not continue it and the word before it is not an abbreviation.
        spaced = end >= length or text[end].isspace()
        if char in _TIGHT_TERMINATORS or (
            spaced and not _continues(text, end) and not _ends_abbreviation(text, position)
        ):
            boundaries.append(end)
        position = end

    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in sorted(set(boundaries)):
        if boundary <= start:
            continue
        spans.append((start, boundary))
        start = boundary
    if start < length:
        spans.append((start, length))

    trimmed = [span for span in (_trim(text, *span) for span in spans) if span is not None]
    if not trimmed:
        whole = _trim(text, 0, length)
        return [whole] if whole is not None else []
    return trimmed


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if end > start else None


def word_spans(text: str) -> list[tuple[int, int]]:
    """Spans of whitespace-delimited words, the finest unit chunking splits on."""
    return [match.span() for match in _WORD.finditer(text)]
