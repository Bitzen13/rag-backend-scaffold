"""The OCR fallback, with a fake engine so no Tesseract install is needed.

What matters is the wiring: a page is only OCR'd when it actually looks scanned, the recognised
words are scaled from image pixels into PDF points, and a missing Tesseract is an error rather
than a silent empty page.
"""

from typing import Any

import pytest

from app.extraction.pdf.clean import OcrSettings
from app.extraction.pdf.ocr import POINTS_PER_INCH, TesseractEngine, looks_scanned, ocr_page_words


class FakePage:
    """The parts of a pdfplumber page the OCR path touches."""

    def __init__(self, text: str = "", images: int = 1) -> None:
        self._text = text
        self.images = [object()] * images
        self.page_number = 1
        self.rendered_at: int | None = None

    def extract_text(self) -> str:
        return self._text

    def to_image(self, resolution: int) -> object:
        self.rendered_at = resolution
        return object()


class FakeEngine:
    def __init__(self, words: list[dict[str, Any]] | None = None) -> None:
        self.words_returned = (
            words
            if words is not None
            else [
                {
                    "text": "Scanned",
                    "x0": 300.0,
                    "x1": 600.0,
                    "top": 150.0,
                    "bottom": 210.0,
                    "size": 60.0,
                    "fontname": "OCR",
                }
            ]
        )
        self.language: str | None = None

    def words(self, image: Any, language: str) -> list[dict[str, Any]]:
        self.language = language
        return [dict(word) for word in self.words_returned]


def settings(**overrides: Any) -> OcrSettings:
    return OcrSettings(enabled=True, **overrides)


def test_a_page_with_an_image_and_no_text_looks_scanned() -> None:
    assert looks_scanned(FakePage(text=""), settings())


def test_a_page_with_text_does_not_look_scanned() -> None:
    assert not looks_scanned(FakePage(text="A page full of real extractable text."), settings())


def test_a_page_with_no_image_does_not_look_scanned() -> None:
    assert not looks_scanned(FakePage(text="", images=0), settings())


def test_the_character_threshold_is_configurable() -> None:
    page = FakePage(text="short")
    assert not looks_scanned(page, settings(min_characters=3))
    assert looks_scanned(page, settings(min_characters=50))


def test_recognised_words_are_scaled_from_pixels_to_points() -> None:
    page = FakePage()
    engine = FakeEngine()
    (word,) = ocr_page_words(page, settings(resolution=300), engine)
    scale = POINTS_PER_INCH / 300
    assert word["x0"] == pytest.approx(300.0 * scale)
    assert word["top"] == pytest.approx(150.0 * scale)
    assert word["size"] == pytest.approx(60.0 * scale)
    assert word["text"] == "Scanned"


def test_the_page_is_rendered_at_the_configured_resolution_and_language() -> None:
    page = FakePage()
    engine = FakeEngine()
    ocr_page_words(page, settings(resolution=200, language="deu"), engine)
    assert page.rendered_at == 200
    assert engine.language == "deu"


def test_an_engine_that_recognises_nothing_returns_no_words() -> None:
    assert ocr_page_words(FakePage(), settings(), FakeEngine([])) == []


def test_a_missing_tesseract_is_reported_rather_than_ignored() -> None:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="pip install pytesseract"):
            TesseractEngine().words(object(), "eng")
    else:
        pytest.skip("pytesseract is installed, so the missing-dependency path cannot be reached")
