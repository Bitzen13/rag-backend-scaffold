"""Extractor contract. Implementations are selected from `extraction.provider` in config.yaml."""

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.db.models import JsonObject

logger = logging.getLogger(__name__)


class ElementKind(StrEnum):
    """The kinds of structural piece every reader reduces its format to.

    Chunking rules are written against these kinds, never against a file format, so a new format
    is a new reader emitting these same elements.
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


class ExtractionError(RuntimeError):
    """One source could not be read. The run reports it and carries on with the others."""


@dataclass(frozen=True, kw_only=True)
class SourceCandidate:
    """A source that exists right now, identified without being parsed.

    Enumerating candidates is deliberately cheap — a hash of the raw bytes, and the name of the
    reader that would parse them — so a run can decide whether a document needs opening at all
    before paying for the parse. Parsing happens in `read()` and nowhere else.
    """

    source_type: str
    source_uri: str  # with source_type, the sources unique key
    source_hash: str  # of the raw bytes; "" when it could not be computed, which forces a read
    reader: str = ""  # the reader that will parse it, known from the extension alone
    reader_version: int = 0  # bumping it re-extracts every document this reader handles

    def read(self) -> ExtractedDocument:
        """Parse this source. Raises ExtractionError if it cannot be read."""
        raise NotImplementedError


class Extractor(Protocol):
    @property
    def source_type(self) -> str:
        """The `sources.source_type` every candidate of this extractor carries."""
        ...

    def candidates(self) -> Iterable[SourceCandidate]:
        """Every source present right now, enumerated without parsing any of them.

        The result is also the set of sources that still exist, which is what a run compares
        against stored state to find the ones that have been deleted.
        """
        ...


def read_all(extractor: Extractor) -> Iterator[ExtractedDocument]:
    """Every source, parsed — for the tools that want all the text and no change detection.

    A source that cannot be read is logged and left out, so one bad file never stops a run.
    """
    for candidate in extractor.candidates():
        try:
            yield candidate.read()
        except ExtractionError as exc:
            logger.warning("skipping %s", exc)
