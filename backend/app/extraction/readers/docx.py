"""Word document reader: headings, paragraphs, lists and tables, in document order.

Word states its own structure in paragraph styles, so there is nothing to infer: "Heading 3" is a
level-3 heading and a numbered paragraph is a list item. The one thing that needs care is order -
python-docx exposes paragraphs and tables as separate collections - so the document body is
walked directly to keep tables where they belong.
"""

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.db.models import JsonObject
from app.extraction.base import DocumentElement, ElementKind
from app.extraction.elements import ElementList
from app.extraction.readers.base import ReaderError, ReadResult, SectionStack, require
from app.extraction.tables import TableReader
from app.extraction.text import squash, tidy_block

logger = logging.getLogger(__name__)

TITLE_LEVEL = 1
CAPTION_MAX_WORDS = 20

_HEADING_STYLE = re.compile(r"^heading\s*(\d+)$", re.IGNORECASE)
_LIST_STYLE = re.compile(r"list", re.IGNORECASE)
_CODE_STYLE = re.compile(r"code|source|preformatted|macro", re.IGNORECASE)


def _style_name(paragraph: Any) -> str:
    style = getattr(paragraph, "style", None)
    return str(getattr(style, "name", "") or "")


def _is_numbered(paragraph: Any) -> bool:
    """True when Word is numbering or bulleting this paragraph itself."""
    properties = paragraph._p.pPr
    return properties is not None and properties.numPr is not None


class DocxReader:
    version = 1  # bump to force re-extraction of documents this reader read

    @property
    def name(self) -> str:
        return "docx"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".docx", ".docm")

    def read(self, path: Path) -> ReadResult:
        docx = require("docx", "python-docx")
        try:
            document = docx.Document(str(path))
        except Exception as exc:  # python-docx raises PackageNotFoundError and others
            raise ReaderError(f"could not open {path.name} as a Word document: {exc}") from exc
        walker = _Walker(docx)
        for block in walker.body(document):
            walker.handle(block)
        document_text, elements = walker.elements.build()
        metadata: JsonObject = {
            "paragraphs": walker.paragraph_count,
            "tables": walker.table_count,
        }
        core = getattr(document, "core_properties", None)
        if core is not None and getattr(core, "title", None):
            metadata["title"] = squash(str(core.title))
        return ReadResult(document_text, elements, metadata)


class _Walker:
    def __init__(self, docx: Any) -> None:
        self._table_class = docx.table.Table
        self._paragraph_class = docx.text.paragraph.Paragraph
        self.elements = ElementList()
        self.paragraph_count = 0
        self.table_count = 0
        self._sections = SectionStack()
        self._last_text = ""
        self._list_lead_in = ""

    def body(self, document: Any) -> Iterator[Any]:
        """Paragraphs and tables in the order they appear in the document."""
        parent = document.element.body
        for child in parent.iterchildren():
            tag = str(child.tag)
            if tag.endswith("}p"):
                yield self._paragraph_class(child, document)
            elif tag.endswith("}tbl"):
                yield self._table_class(child, document)

    def handle(self, block: Any) -> None:
        if isinstance(block, self._table_class):
            self._table(block)
        else:
            self._paragraph(block)

    def _add(
        self,
        kind: ElementKind,
        text: str,
        *,
        language: str = "",
        lead_in: str = "",
    ) -> None:
        if not text:
            return
        self.elements.add(
            DocumentElement(
                kind=kind,
                text=text,
                section=self._sections.path(),
                language=language,
                lead_in=lead_in,
            )
        )

    def _paragraph(self, paragraph: Any) -> None:
        raw = paragraph.text
        style = _style_name(paragraph)
        heading = _HEADING_STYLE.match(style)
        if style.lower() == "title":
            text = squash(raw)
            if text:
                self.paragraph_count += 1
                self._push(TITLE_LEVEL, text)
            return
        if heading is not None:
            text = squash(raw)
            if text:
                self.paragraph_count += 1
                self._push(int(heading.group(1)), text)
            return
        if _CODE_STYLE.search(style):
            self.paragraph_count += 1
            self._add(ElementKind.CODE, tidy_block(raw))
            return
        text = squash(raw)
        if not text:
            return
        self.paragraph_count += 1
        if _is_numbered(paragraph) or _LIST_STYLE.search(style):
            lead_in = self._list_lead_in or (
                self._last_text if self._last_text.endswith(":") else ""
            )
            self._list_lead_in = lead_in
            self._add(ElementKind.LIST_ITEM, text, lead_in=lead_in)
            return
        self._list_lead_in = ""
        self._last_text = text
        self._add(ElementKind.PARAGRAPH, text)

    def _push(self, level: int, title: str) -> None:
        self._sections.push(level, title)
        self._last_text = ""
        self._list_lead_in = ""
        self._add(ElementKind.HEADING, title)

    def _table(self, table: Any) -> None:
        self.table_count += 1
        caption = self._last_text if len(self._last_text.split()) <= CAPTION_MAX_WORDS else ""
        reader = TableReader(table=caption, section=self._sections.path())
        for row in table.rows:
            element = reader.add([squash(cell.text) for cell in row.cells])
            if element is not None:
                self.elements.add(element)
        self._last_text = ""
