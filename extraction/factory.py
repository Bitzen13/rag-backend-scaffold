from extraction.base import Extractor
from extraction.filesystem import FilesystemExtractor
from extraction.pdf import OcrSettings, PdfOptions
from settings import ExtractionConfig


def build_pdf_options(config: ExtractionConfig) -> PdfOptions:
    pdf = config.pdf
    return PdfOptions(
        strip_patterns=pdf.strip_patterns,
        truncate_patterns=pdf.truncate_patterns,
        skip_sections=pdf.skip_sections,
        borderless_tables=pdf.borderless_tables,
        ocr=OcrSettings(
            enabled=pdf.ocr.enabled,
            language=pdf.ocr.language,
            min_characters=pdf.ocr.min_characters,
            resolution=pdf.ocr.resolution,
        ),
    )


def build_extractor(config: ExtractionConfig) -> Extractor:
    if config.provider == "filesystem":
        return FilesystemExtractor(config.path, config.extensions, build_pdf_options(config))
    raise ValueError(f"unsupported extraction provider: {config.provider}")
