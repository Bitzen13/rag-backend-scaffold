"""Assembling a reader's output: element order, offsets, and the document text they belong to.

Every reader ends by handing its elements to `ElementList.build()`, so offsets are computed in
one place and `ExtractedDocument.text` is always the elements joined in reading order.
"""

from collections.abc import Iterable
from dataclasses import replace

from app.extraction.base import DocumentElement

ELEMENT_SEPARATOR = "\n"


class ElementList:
    """Collects elements, then renders them into document text with each one's offsets in it."""

    def __init__(self) -> None:
        self._elements: list[DocumentElement] = []

    def add(self, element: DocumentElement) -> None:
        """Append an element, ignoring it when its text is empty or only whitespace."""
        if element.text.strip():
            self._elements.append(element)

    def extend(self, elements: Iterable[DocumentElement]) -> None:
        for element in elements:
            self.add(element)

    def __len__(self) -> int:
        return len(self._elements)

    def build(self) -> tuple[str, tuple[DocumentElement, ...]]:
        """Return (document text, elements with char_start/char_end pointing into that text)."""
        return place_elements(self._elements)


def place_elements(elements: Iterable[DocumentElement]) -> tuple[str, tuple[DocumentElement, ...]]:
    """Join element texts and give each element its offsets in the joined text."""
    placed: list[DocumentElement] = []
    position = 0
    for element in elements:
        end = position + len(element.text)
        placed.append(replace(element, char_start=position, char_end=end))
        position = end + len(ELEMENT_SEPARATOR)
    return ELEMENT_SEPARATOR.join(e.text for e in placed), tuple(placed)
