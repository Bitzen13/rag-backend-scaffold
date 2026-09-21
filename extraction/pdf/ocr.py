"""Optional OCR for PDF pages that hold an image of text rather than text.

Off unless `extraction.pdf.ocr.enabled` is set. When a page yields almost no characters but does
carry an image, it is rendered and read with Tesseract, which produces words with positions and
heights - the same shape the layout analysis already works on, so an OCR'd page goes through
exactly the same heading, column and table detection as any other page.

Enabled but unavailable is an error, not a silent fallback: a scanned corpus that quietly ingests
as empty is worse than a run that stops and says Tesseract is missing.
"""

import logging
from typing import Any, Protocol

from extraction.pdf.clean import OcrSettings

logger = logging.getLogger(__name__)

POINTS_PER_INCH = 72.0
MIN_CONFIDENCE = 40.0  # per-word Tesseract confidence, 0-100
DEFAULT_FONT_NAME = "OCR"

Word = dict[str, Any]


class OcrEngine(Protocol):
    def words(self, image: Any, language: str) -> list[Word]:
        """Recognised words as {text, x0, x1, top, bottom, size, fontname}, in image pixels."""
        ...


class TesseractEngine:
    """Tesseract via pytesseract. Word heights stand in for font sizes."""

    def words(self, image: Any, language: str) -> list[Word]:
        try:
            import pytesseract
        except ImportError as exc:
            raise RuntimeError(
                "extraction.pdf.ocr.enabled is set but pytesseract is not installed: "
                "pip install pytesseract (and install the tesseract binary)"
            ) from exc
        try:
            data = pytesseract.image_to_data(
                image, lang=language, output_type=pytesseract.Output.DICT
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise RuntimeError(
                "extraction.pdf.ocr.enabled is set but the tesseract binary was not found: "
                "install Tesseract and make sure it is on PATH"
            ) from exc
        words: list[Word] = []
        for index, text in enumerate(data["text"]):
            stripped = str(text).strip()
            if not stripped or float(data["conf"][index]) < MIN_CONFIDENCE:
                continue
            left, top = float(data["left"][index]), float(data["top"][index])
            width, height = float(data["width"][index]), float(data["height"][index])
            words.append(
                {
                    "text": stripped,
                    "x0": left,
                    "x1": left + width,
                    "top": top,
                    "bottom": top + height,
                    "size": height,
                    "fontname": DEFAULT_FONT_NAME,
                }
            )
        return words


def looks_scanned(page: Any, settings: OcrSettings) -> bool:
    """True when a page carries an image but next to no extractable text."""
    text = page.extract_text() or ""
    return len(text.strip()) < settings.min_characters and bool(page.images)


def ocr_page_words(page: Any, settings: OcrSettings, engine: OcrEngine) -> list[Word]:
    """Render a page and read it with OCR, returning words in PDF points."""
    image = page.to_image(resolution=settings.resolution)
    recognised = engine.words(getattr(image, "original", image), settings.language)
    scale = POINTS_PER_INCH / settings.resolution
    for word in recognised:
        for key in ("x0", "x1", "top", "bottom", "size"):
            word[key] = float(word[key]) * scale
    logger.info(
        "page %s: OCR recovered %d words", getattr(page, "page_number", "?"), len(recognised)
    )
    return recognised
