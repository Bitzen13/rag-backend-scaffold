"""Filesystem extractor: walks a directory tree and hands each file to the right reader."""

import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from db.models import JsonObject
from extraction.base import ExtractedDocument
from extraction.pdf import PdfOptions
from extraction.readers.base import ReaderError
from extraction.readers.registry import ReaderRegistry, build_registry

logger = logging.getLogger(__name__)

SOURCE_TYPE = "file"


class FilesystemExtractor:
    """Reads every file under a directory whose extension is listed in `extraction.extensions`.

    The extractor decides which files to visit; the reader for each one decides how to describe
    it. A file that cannot be read is logged and skipped, so one bad file does not stop a run.
    """

    def __init__(
        self,
        root: Path,
        extensions: Sequence[str],
        pdf_options: PdfOptions | None = None,
        registry: ReaderRegistry | None = None,
    ) -> None:
        self._root = root
        self._extensions = frozenset(e.lower() for e in extensions)
        self._registry = registry or build_registry(pdf_options)

    def extract(self) -> Iterator[ExtractedDocument]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"extraction path is not a directory: {self._root}")
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            document = self._read(path)
            if document is not None:
                yield document

    def _read(self, path: Path) -> ExtractedDocument | None:
        reader = self._registry.for_path(path)
        try:
            result = reader.read(path)
        except (OSError, UnicodeDecodeError, ReaderError) as exc:
            logger.warning("skipping %s: %s", path, exc)
            return None
        except Exception:
            logger.exception("skipping %s: the %s reader failed", path, reader.name)
            return None
        metadata: JsonObject = {
            "filename": path.name,
            "extension": path.suffix.lower(),
            "reader": reader.name,
            **result.metadata,
        }
        if not result.text.strip():
            logger.warning("%s: the %s reader found no text", path.name, reader.name)
        return ExtractedDocument(
            source_type=SOURCE_TYPE,
            source_uri=path.relative_to(self._root).as_posix(),
            text=result.text,
            metadata=metadata,
            elements=result.elements,
        )
