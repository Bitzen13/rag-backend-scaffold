import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from extraction.base import ExtractedDocument

logger = logging.getLogger(__name__)

SOURCE_TYPE = "file"


class FilesystemExtractor:
    """Reads UTF-8 text files (e.g. .md, .txt) from a directory tree."""

    def __init__(self, root: Path, extensions: Sequence[str]) -> None:
        self._root = root
        self._extensions = frozenset(e.lower() for e in extensions)

    def extract(self) -> Iterator[ExtractedDocument]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"extraction path is not a directory: {self._root}")
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("skipping unreadable file %s: %s", path, exc)
                continue
            yield ExtractedDocument(
                source_type=SOURCE_TYPE,
                source_uri=path.relative_to(self._root).as_posix(),
                text=text,
                metadata={"filename": path.name, "extension": path.suffix.lower()},
            )
