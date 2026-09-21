"""Reader contract: one reader per file format, all producing the same elements.

A reader's whole job is to describe a file in terms of `DocumentElement`s. It makes no chunking
decisions, so adding a format never touches the chunker (see context/adding-a-reader.md).
"""

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol

from db.models import JsonObject
from extraction.base import DocumentElement

logger = logging.getLogger(__name__)

SECTION_SEPARATOR = " > "


class ReaderError(RuntimeError):
    """A file could not be read as the format its extension claims."""


class MissingDependencyError(ReaderError):
    """A reader needs a package that is not installed."""


@dataclass(frozen=True)
class ReadResult:
    """What a reader returns: the document text, its elements, and format-specific metadata."""

    text: str
    elements: tuple[DocumentElement, ...] = ()
    metadata: JsonObject = field(default_factory=dict)


class Reader(Protocol):
    @property
    def name(self) -> str:
        """Recorded in the document's metadata, so it is clear which reader produced it."""
        ...

    @property
    def extensions(self) -> tuple[str, ...]:
        """Lower-case file extensions this reader handles, including the leading dot."""
        ...

    @property
    def version(self) -> int:
        """Bumped by hand whenever this reader's output changes.

        A reader improvement changes the text extracted from a file without changing the file
        itself, so its raw hash still matches and it would never be re-read. The version is
        recorded with each source, and bumping it forces re-extraction of every document this
        reader handles (see context/adding-a-reader.md).
        """
        ...

    def read(self, path: Path) -> ReadResult:
        """Read one file. Raises ReaderError if the file is not usable as this format."""
        ...


def require(module: str, package: str) -> ModuleType:
    """Import an optional reader dependency, or explain how to install it."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise MissingDependencyError(
            f"this file needs the {package} package: pip install {package}"
        ) from exc


class SectionStack:
    """The heading path above the current element, for any format with levelled headings."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, title))

    def clear(self) -> None:
        self._stack.clear()

    def path(self) -> str:
        return SECTION_SEPARATOR.join(title for _, title in self._stack)
