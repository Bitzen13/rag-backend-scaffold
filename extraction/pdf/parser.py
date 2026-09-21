import logging
from pathlib import Path

import pdfplumber

from extraction.base import DocumentElement
from extraction.pdf.builder import StructureBuilder
from extraction.pdf.clean import PdfOptions, TextCleaner
from extraction.pdf.layout import analyze_page
from extraction.pdf.model import PageFlow
from extraction.pdf.stats import collect_stats

logger = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)  # noisy font warnings on web-printed PDFs

PAGE_LOG_INTERVAL = 10  # log parsing progress every this many pages


def document_text(elements: list[DocumentElement]) -> str:
    return "\n".join(e.text for e in elements)


def parse_pdf(path: Path, options: PdfOptions | None = None) -> tuple[list[DocumentElement], int]:
    """Read a PDF into ordered elements (headings, paragraphs, table rows); returns them with the
    page count. Layout is analysed per page, then judged against whole-document statistics."""
    options = options or PdfOptions()
    pages: list[PageFlow] = []
    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        logger.info("parsing %s (%d pages)", path.name, total_pages)
        for number, page in enumerate(pdf.pages, start=1):
            if number % PAGE_LOG_INTERVAL == 0:
                logger.info("parsing %s: page %d/%d", path.name, number, total_pages)
            pages.append(analyze_page(page, number))
            page.flush_cache()

    scanned = [p.number for p in pages if not p.items and p.has_images]
    if scanned:
        logger.warning(
            "%s: %d page(s) have images but no extractable text (scanned?) and were skipped: %s",
            path.name,
            len(scanned),
            scanned[:10],
        )

    cleaner = TextCleaner(options)
    builder = StructureBuilder(
        stats=collect_stats(pages),
        cleaner=cleaner,
        skip_sections=frozenset(s.strip().lower() for s in options.skip_sections),
    )
    for page_flow in pages:
        builder.add_page(page_flow)
    elements = builder.finish()
    if cleaner.removed_characters:
        logger.warning(
            "%s: removed %d invalid characters (e.g. NUL) that the database cannot store",
            path.name,
            cleaner.removed_characters,
        )
    return elements, total_pages
