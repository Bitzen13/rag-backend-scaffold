"""Extractor contract. Implementations are selected from `extraction.provider` in config.yaml."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from db.models import JsonObject


class ElementKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE_ROW = "table_row"


@dataclass(frozen=True)
class DocumentElement:
    """One structural piece of a document, for chunkers that respect document structure.

    `text` is the element's rendered text; its slice of `ExtractedDocument.text` is
    [char_start, char_end). A table row's `text` is its `Header: value | ...` rendering and
    `fields` holds the (header, value) pairs.
    """

    kind: ElementKind
    text: str
    page: int
    section: str = ""  # nearest heading above the element
    table: str = ""  # table title, for table rows
    fields: tuple[tuple[str, str], ...] = ()
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
