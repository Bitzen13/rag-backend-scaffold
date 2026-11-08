"""Building the extractor named by `extraction.provider`.

`filesystem` is the one the shared codebase ships. A client whose data lives somewhere the shared
code cannot enumerate registers its own under a name of its choosing, through its customisation
module (customisations/README.md, context/adding-an-extractor.md), and selects it with the same
config value. The dispatch is here so there is one place that knows what a provider name can be.
"""

from customisations import Customisation
from extraction.base import Extractor
from extraction.filesystem import FilesystemExtractor
from extraction.limits import FileLimits
from extraction.pdf import OcrSettings, PdfOptions
from settings import ExtractionConfig

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


def build_extractor(
    config: ExtractionConfig, customisation: Customisation | None = None
) -> Extractor:
    """The extractor for this deployment.

    Raises ValueError naming what is available for an unknown provider. It cannot be a load-time
    error, because which providers exist depends on the customisation module, which settings
    deliberately does not import — but it is raised while the extractor is built, at the start of
    a run, rather than part way through one.
    """
    client = customisation or Customisation()
    if config.provider == FILESYSTEM:
        return FilesystemExtractor(
            config.path,
            config.extensions,
            build_pdf_options(config),
            extra_readers=client.readers,
            limits=build_file_limits(config),
        )
    factory = client.extractors.get(config.provider)
    if factory is not None:
        return factory(config)
    available = ", ".join(sorted({FILESYSTEM, *client.extractors}))
    raise ValueError(
        f"unsupported extraction provider: {config.provider!r} (available: {available})"
    )
