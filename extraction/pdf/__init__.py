"""Structure-preserving PDF reader: headings, paragraphs and table rows, judged per document."""

from extraction.pdf.clean import OcrSettings, PdfOptions
from extraction.pdf.parser import parse_pdf

__all__ = ["OcrSettings", "PdfOptions", "parse_pdf"]
