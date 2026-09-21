"""Structure-aware chunking: one chunk per table row, paragraphs packed under their heading.

Needs a document with `elements` (see extraction/pdf/). A document with no structure falls back
to fixed-size windows.
"""

from dataclasses import dataclass

from chunking.base import TextChunk
from chunking.fixed_size import FixedSizeChunker
from db.models import JsonObject
from extraction.base import DocumentElement, ElementKind, ExtractedDocument


@dataclass(frozen=True)
class _Piece:
    text: str
    char_start: int
    char_end: int
    metadata: JsonObject


def _words(text: str) -> int:
    return len(text.split())


class DocumentAwareChunker:
    def __init__(self, chunk_size: int, chunk_overlap: int) -> None:
        self._size = chunk_size
        self._splitter = FixedSizeChunker(chunk_size, chunk_overlap)

    def chunk(self, document: ExtractedDocument) -> list[TextChunk]:
        if not document.elements:
            return self._splitter.chunk(document)

        pieces: list[_Piece] = []
        pending: list[DocumentElement] = []

        def flush() -> None:
            if pending:
                pieces.append(self._prose_piece(pending))
                pending.clear()

        for element in document.elements:
            if element.kind is ElementKind.HEADING:
                continue  # carried as `section` on the elements beneath it
            if element.kind is ElementKind.TABLE_ROW:
                flush()
                pieces.extend(self._row_pieces(element))
            elif _words(element.text) > self._size:
                flush()
                pieces.extend(self._split_long_paragraph(element))
            else:
                if pending and (
                    pending[0].section != element.section
                    or sum(_words(p.text) for p in pending) + _words(element.text) > self._size
                ):
                    flush()
                pending.append(element)
        flush()

        return [
            TextChunk(
                index=i,
                text=p.text,
                token_count=_words(p.text),
                char_start=p.char_start,
                char_end=p.char_end,
                metadata=p.metadata,
            )
            for i, p in enumerate(pieces)
        ]

    @staticmethod
    def _prose_metadata(element: DocumentElement) -> JsonObject:
        return {"chunk_type": "prose", "section": element.section, "page": element.page}

    def _prose_piece(self, elements: list[DocumentElement]) -> _Piece:
        first = elements[0]
        body = "\n\n".join(e.text for e in elements)
        return _Piece(
            text=f"{first.section}\n{body}" if first.section else body,
            char_start=first.char_start,
            char_end=elements[-1].char_end,
            metadata=self._prose_metadata(first),
        )

    def _split_long_paragraph(self, element: DocumentElement) -> list[_Piece]:
        pieces: list[_Piece] = []
        for part in self._splitter.split_text(element.text):
            text = f"{element.section}\n{part.text}" if element.section else part.text
            pieces.append(
                _Piece(
                    text=text,
                    char_start=element.char_start + part.char_start,
                    char_end=element.char_start + part.char_end,
                    metadata=self._prose_metadata(element),
                )
            )
        return pieces

    def _row_pieces(self, element: DocumentElement) -> list[_Piece]:
        # The table title usually already names the section (e.g. "...in the Milky Way").
        redundant = element.section.lower() in element.table.lower()
        context = " / ".join(x for x in (element.table, "" if redundant else element.section) if x)
        name = element.fields[0][1] if element.fields else ""
        metadata: JsonObject = {
            "chunk_type": "table_row",
            "table": element.table,
            "section": element.section,
            "page": element.page,
            "row_name": name,
        }

        def build(body: str) -> str:
            return f"{context}\n{body}" if context else body

        whole = build(element.text)
        if _words(whole) <= self._size:
            return [_Piece(whole, element.char_start, element.char_end, metadata)]

        # A row with a very long cell: split it, repeating the context and row name on each part.
        parts = self._splitter.split_text(element.text)
        pieces: list[_Piece] = []
        for number, part in enumerate(parts, start=1):
            label = f"{name} (part {number}/{len(parts)})" if name else f"(part {number})"
            pieces.append(
                _Piece(
                    text=build(f"{label}\n{part.text}"),
                    char_start=element.char_start + part.char_start,
                    char_end=element.char_start + part.char_end,
                    metadata=metadata,
                )
            )
        return pieces
