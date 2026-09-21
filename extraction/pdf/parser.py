import logging
from pathlib import Path

import pdfplumber

from extraction.base import DocumentElement
from extraction.pdf.builder import StructureBuilder
from extraction.pdf.clean import PdfOptions, TextCleaner
from extraction.pdf.layout import analyze_page
from extraction.pdf.model import PageFlow
from extraction.pdf.ocr import OcrEngine, TesseractEngine, looks_scanned, ocr_page_words
from extraction.pdf.stats import collect_stats

logger = logging.getLogger(__name__)
logging.getLogger("pdfminer").setLevel(logging.ERROR)  # noisy font warnings on web-printed PDFs

PAGE_LOG_INTERVAL = 10  # log parsing progress every this many pages


def parse_pdf(
    path: Path, options: PdfOptions | None = None, *, ocr_engine: OcrEngine | None = None
) -> tuple[list[DocumentElement], int, str]:
    """Read a PDF into ordered elements (headings, paragraphs, table rows).

    Returns the elements, the page count, and the document text the elements' offsets point into.
    Layout is analysed per page, then judged against whole-document statistics.
    """
    options = options or PdfOptions()
    engine = ocr_engine or TesseractEngine()
    pages: list[PageFlow] = []
    scanned: list[int] = []
    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        logger.info("parsing %s (%d pages)", path.name, total_pages)
        for number, page in enumerate(pdf.pages, start=1):
            if number % PAGE_LOG_INTERVAL == 0:
                logger.info("parsing %s: page %d/%d", path.name, number, total_pages)
            words = None
            if options.ocr.enabled and looks_scanned(page, options.ocr):
                scanned.append(number)
                words = ocr_page_words(page, options.ocr, engine)
            flow = analyze_page(
                page, number, borderless_tables=options.borderless_tables, words=words
            )
            pages.append(flow)
            page.flush_cache()

    if options.ocr.enabled and scanned:
        logger.info("%s: read %d page(s) with OCR: %s", path.name, len(scanned), scanned[:10])
    missed = [p.number for p in pages if not p.items and p.has_images]
    if missed:
        logger.warning(
            "%s: %d page(s) have images but no extractable text (scanned?) and were skipped: %s%s",
            path.name,
            len(missed),
            missed[:10],
            "" if options.ocr.enabled else "; set extraction.pdf.ocr.enabled to read them",
        )

    cleaner = TextCleaner(options)
    builder = StructureBuilder(
        stats=collect_stats(pages),
        cleaner=cleaner,
        skip_sections=frozenset(s.strip().lower() for s in options.skip_sections),
    )
    for page_flow in pages:
        builder.add_page(page_flow)
    text, elements = builder.finish()
    if cleaner.removed_characters:
        logger.warning(
            "%s: removed %d invalid characters (e.g. NUL) that the database cannot store",
            path.name,
            cleaner.removed_characters,
        )
    return elements, total_pages, text
