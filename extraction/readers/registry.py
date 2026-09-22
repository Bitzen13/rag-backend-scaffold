"""Which reader handles which file.

One place maps an extension to a reader, and one reader is the fallback, so a file type nobody
has written a reader for is still ingested (as plain text with light structure detection) rather
than skipped. Adding a format means adding a reader here and nothing else.

`extra` readers come from a client's customisation module and are registered first, so a client
can claim an extension the shared code also handles without changing the shared code (see
customisations/README.md).
"""

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from extraction.pdf import PdfOptions
from extraction.readers.base import Reader
from extraction.readers.code import CodeReader
from extraction.readers.docx import DocxReader
from extraction.readers.email import EmailReader
from extraction.readers.html import HtmlReader
from extraction.readers.markdown import MarkdownReader
from extraction.readers.pdf import PdfReader
from extraction.readers.plaintext import PlainTextReader
from extraction.readers.structured import StructuredReader
from extraction.readers.tabular import CsvReader, ExcelReader

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
    pdf_options: PdfOptions | None = None, extra: Sequence[Reader] = ()
) -> ReaderRegistry:
    fallback = PlainTextReader()
    return ReaderRegistry(
        [
            # First wins (`setdefault` per extension), so a client's reader takes precedence.
            *extra,
            PdfReader(pdf_options),
            MarkdownReader(),
            HtmlReader(),
            DocxReader(),
            CsvReader(),
            ExcelReader(),
            StructuredReader(),
            EmailReader(),
            CodeReader(),
            fallback,
        ],
        fallback,
    )
