"""Structure-preserving PDF reader: headings, paragraphs and table rows, judged per document."""

from app.extraction.pdf.clean import OcrSettings, PdfOptions
from app.extraction.pdf.parser import parse_pdf

__all__ = ["OcrSettings", "PdfOptions", "parse_pdf"]
