"""Building the extractor named by `extraction.provider` (`filesystem` is the only one).

A new data source is a new `Extractor` implementation (`extraction/base.py`) and a new name here
and in `ExtractionConfig.provider`.
"""

from app.extraction.base import Extractor
from app.extraction.filesystem import FilesystemExtractor
from app.extraction.limits import FileLimits
from app.extraction.pdf import OcrSettings, PdfOptions
from app.settings import ExtractionConfig

FILESYSTEM = "filesystem"


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


def build_file_limits(config: ExtractionConfig) -> FileLimits:
    limits = config.limits
    return FileLimits(
        max_file_bytes=limits.max_file_bytes,
        max_archive_uncompressed_bytes=limits.max_archive_uncompressed_bytes,
        max_spreadsheet_cells=limits.max_spreadsheet_cells,
    )


def build_extractor(config: ExtractionConfig) -> Extractor:
    """The extractor for this deployment. Settings validation guarantees the provider exists."""
    if config.provider != FILESYSTEM:
        raise ValueError(f"unsupported extraction provider: {config.provider!r}")
    return FilesystemExtractor(
        config.path,
        config.extensions,
        build_pdf_options(config),
        limits=build_file_limits(config),
    )
