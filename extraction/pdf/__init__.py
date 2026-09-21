"""Structure-preserving PDF reader: headings, paragraphs and table rows, judged per document."""

from extraction.pdf.clean import PdfOptions
from extraction.pdf.parser import document_text, parse_pdf

__all__ = ["PdfOptions", "document_text", "parse_pdf"]
