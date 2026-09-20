from extraction.base import Extractor
from extraction.filesystem import FilesystemExtractor
from settings import ExtractionConfig


def build_extractor(config: ExtractionConfig) -> Extractor:
    if config.provider == "filesystem":
        return FilesystemExtractor(config.path, config.extensions)
    raise ValueError(f"unsupported extraction provider: {config.provider}")
