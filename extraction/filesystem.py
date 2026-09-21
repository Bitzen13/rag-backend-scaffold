import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from pdfplumber.utils.exceptions import PdfminerException

from db.models import JsonObject
from extraction.base import ExtractedDocument
from extraction.pdf import PdfOptions, document_text, parse_pdf
from extraction.text import strip_invalid_characters

logger = logging.getLogger(__name__)

SOURCE_TYPE = "file"


class FilesystemExtractor:
    """Reads UTF-8 text files (.md, .txt, ...) and PDFs from a directory tree."""

    def __init__(
        self, root: Path, extensions: Sequence[str], pdf_options: PdfOptions | None = None
    ) -> None:
        self._root = root
        self._pdf_options = pdf_options or PdfOptions()
        self._extensions = frozenset(e.lower() for e in extensions)

    def extract(self) -> Iterator[ExtractedDocument]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"extraction path is not a directory: {self._root}")
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            try:
                document = self._read(path)
            except (OSError, UnicodeDecodeError, PdfminerException) as exc:
                logger.warning("skipping unreadable file %s: %s", path, exc)
                continue
            yield document

    def _read(self, path: Path) -> ExtractedDocument:
        source_uri = path.relative_to(self._root).as_posix()
        metadata: JsonObject = {"filename": path.name, "extension": path.suffix.lower()}
        if path.suffix.lower() == ".pdf":
            elements, pages = parse_pdf(path, self._pdf_options)
            return ExtractedDocument(
                source_type=SOURCE_TYPE,
                source_uri=source_uri,
                text=document_text(elements),
                metadata={**metadata, "pages": pages},
                elements=tuple(elements),
            )
        text, removed = strip_invalid_characters(path.read_text(encoding="utf-8"))
        if removed:
            logger.warning("%s: removed %d invalid characters (e.g. NUL)", path.name, removed)
        return ExtractedDocument(
            source_type=SOURCE_TYPE, source_uri=source_uri, text=text, metadata=metadata
        )
