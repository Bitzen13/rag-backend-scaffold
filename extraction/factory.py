from extraction.base import Extractor
from extraction.filesystem import FilesystemExtractor
from extraction.pdf import PdfOptions
from settings import ExtractionConfig


def build_extractor(config: ExtractionConfig) -> Extractor:
    if config.provider == "filesystem":
        pdf_options = PdfOptions(
            strip_patterns=config.pdf.strip_patterns,
            truncate_patterns=config.pdf.truncate_patterns,
            skip_sections=config.pdf.skip_sections,
        )
        return FilesystemExtractor(config.path, config.extensions, pdf_options)
    raise ValueError(f"unsupported extraction provider: {config.provider}")
