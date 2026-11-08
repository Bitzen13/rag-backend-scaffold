"""Text hygiene at the extraction boundary, before anything reaches chunking or the database."""

import re

# Characters that must never be stored: NUL (Postgres text columns reject it), other control
# characters that are noise, and lone surrogates (not valid UTF-8). Ordinary whitespace (tab,
# newline, carriage return, vertical tab, form feed) is kept.
_INVALID_CODES = (
    *range(0x00, 0x09),
    *range(0x0E, 0x1C),
    0x7F,
    *range(0xD800, 0xE000),
)
_STRIP_TABLE: dict[int, None] = dict.fromkeys(_INVALID_CODES)

_SPACES = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


def strip_invalid_characters(text: str) -> tuple[str, int]:
    """Return the text without unstorable characters, and how many were removed."""
    cleaned = text.translate(_STRIP_TABLE)
    return cleaned, len(text) - len(cleaned)


def squash(text: str) -> str:
    """Collapse all whitespace runs (including newlines) to single spaces."""
    return _SPACES.sub(" ", text).strip()


def tidy_block(text: str) -> str:
    """Normalise a multi-line block: keep its line structure, drop trailing and excess blank lines.

    Used for elements whose line breaks carry meaning (code, transcripts), where `squash` would
    destroy them.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACE.sub("\n", text)
    return _BLANK_LINES.sub("\n\n", text).strip("\n")


# Tried in order when a text file's encoding is unknown. utf-8-sig also covers plain UTF-8 and
# strips a byte-order mark; latin-1 cannot fail, so decoding always succeeds rather than dropping
# a file that is merely in an unexpected encoding.
FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode file bytes, returning the text and the encoding that worked."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for encoding in FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def normalize_newlines(text: str) -> str:
    """Windows and classic-Mac line endings become "\n", so line-based parsing is uniform."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
