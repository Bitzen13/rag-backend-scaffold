"""Extractor contract. Implementations are selected from `extraction.provider` in config.yaml."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from db.models import JsonObject


class ElementKind(StrEnum):
    """The kinds of structural piece every reader reduces its format to.

    Chunking rules are written against these kinds, never against a file format, so a new format
    is a new reader emitting these same elements (see context/adding-a-reader.md).
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    CODE = "code"
    QUESTION = "question"
    ANSWER = "answer"
    SPEAKER_TURN = "speaker_turn"
    MESSAGE = "message"


@dataclass(frozen=True)
class DocumentElement:
    """One structural piece of a document, for chunkers that respect document structure.

    `text` is the element's rendered text; its slice of `ExtractedDocument.text` is
    [char_start, char_end). The remaining fields are the element's context: whatever a chunk built
    from it needs in order to make sense on its own once it is retrieved out of the document.
    """

    kind: ElementKind
    text: str
    page: int = 0  # page, sheet or slide number; 0 for formats that have none
    section: str = ""  # heading path above the element, e.g. "Rocket > Engines"
    table: str = ""  # table caption or sheet name, for table rows
    fields: tuple[tuple[str, str], ...] = ()  # (header, value) pairs, for table rows
    lead_in: str = ""  # the line introducing a list, for list items
    speaker: str = ""  # speaker or sender, for transcript turns and messages
    language: str = ""  # language name, for code blocks
    time_seconds: float | None = None  # transcript timestamp, when the source has one
    line_start: int = 0  # 1-based line range in the source file; 0 when not known
    line_end: int = 0
    char_start: int = 0
    char_end: int = 0


@dataclass(frozen=True)
class ExtractedDocument:
    """Clean text pulled from one source, plus the identity used to track it."""

    source_type: str
    source_uri: str  # stable identifier; with source_type it is the sources unique key
    text: str
    metadata: JsonObject = field(default_factory=dict)
    elements: tuple[DocumentElement, ...] = ()  # empty for sources with no known structure


class Extractor(Protocol):
    def extract(self) -> Iterable[ExtractedDocument]:
        """Yield every document currently present in the source."""
        ...
