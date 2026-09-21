"""Text hygiene at the extraction boundary, before anything reaches chunking or the database."""

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


def strip_invalid_characters(text: str) -> tuple[str, int]:
    """Return the text without unstorable characters, and how many were removed."""
    cleaned = text.translate(_STRIP_TABLE)
    return cleaned, len(text) - len(cleaned)
