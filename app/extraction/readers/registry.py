"""Which reader handles which file.

One place maps an extension to a reader, and one reader is the fallback, so a file type nobody
has written a reader for is still ingested (as plain text with light structure detection) rather
than skipped. Adding a format means adding a reader here and nothing else.

`extra` readers are registered first, so a caller can claim an extension a built-in reader also
handles.
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from app.extraction.limits import FileLimits
from app.extraction.pdf import PdfOptions
from app.extraction.readers.base import Reader
from app.extraction.readers.code import CodeReader
from app.extraction.readers.docx import DocxReader
from app.extraction.readers.email import EmailReader
from app.extraction.readers.html import HtmlReader
from app.extraction.readers.markdown import MarkdownReader
from app.extraction.readers.pdf import PdfReader
from app.extraction.readers.plaintext import PlainTextReader
from app.extraction.readers.structured import StructuredReader
from app.extraction.readers.tabular import CsvReader, ExcelReader

logger = logging.getLogger(__name__)


class ReaderRegistry:
    def __init__(self, readers: Sequence[Reader], fallback: Reader) -> None:
        self._fallback = fallback
        self._by_extension: dict[str, Reader] = {}
        for reader in readers:
            for extension in reader.extensions:
                self._by_extension.setdefault(extension.lower(), reader)

    def for_path(self, path: Path) -> Reader:
        """The reader for this file, or the plain-text fallback for an unknown extension."""
        reader = self._by_extension.get(path.suffix.lower())
        if reader is None:
            logger.debug("no reader for %s; reading it as plain text", path.suffix or path.name)
            return self._fallback
        return reader

    def extensions(self) -> tuple[str, ...]:
        """Every extension a dedicated reader handles, for documentation and config defaults."""
        return tuple(sorted(self._by_extension))

    def readers(self) -> Iterable[Reader]:
        return dict.fromkeys(self._by_extension.values())


def build_registry(
    pdf_options: PdfOptions | None = None,
    extra: Sequence[Reader] = (),
    limits: FileLimits | None = None,
) -> ReaderRegistry:
    fallback = PlainTextReader()
    limits = limits or FileLimits()
    return ReaderRegistry(
        [
            # First wins (`setdefault` per extension), so a client's reader takes precedence.
            *extra,
            PdfReader(pdf_options),
            MarkdownReader(),
            HtmlReader(),
            DocxReader(),
            CsvReader(),
            ExcelReader(max_cells=limits.max_spreadsheet_cells),
            StructuredReader(),
            EmailReader(),
            CodeReader(),
            fallback,
        ],
        fallback,
    )
