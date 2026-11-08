"""PDF reader: an adapter over extraction/pdf, which does the structural analysis."""

from pathlib import Path

from app.extraction.pdf import PdfOptions, parse_pdf
from app.extraction.readers.base import ReadResult


class PdfReader:
    """Reads PDFs, including their tables, headings and reading order (see extraction/pdf)."""

    version = 1  # bump to force re-extraction of documents this reader read

    def __init__(self, options: PdfOptions | None = None) -> None:
        self._options = options or PdfOptions()

    @property
    def name(self) -> str:
        return "pdf"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".pdf",)

    def read(self, path: Path) -> ReadResult:
        elements, pages, text = parse_pdf(path, self._options)
        return ReadResult(text, tuple(elements), {"pages": pages})
