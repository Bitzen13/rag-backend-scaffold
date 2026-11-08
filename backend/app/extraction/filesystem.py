"""Filesystem extractor: walks a directory tree and hands each file to the right reader."""

import hashlib
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.db.models import JsonObject
from app.extraction.base import ExtractedDocument, ExtractionError, SourceCandidate
from app.extraction.limits import (
    FileLimits,
    LimitExceededError,
    check_archive,
    check_size,
    outside_root,
)
from app.extraction.pdf import PdfOptions
from app.extraction.readers.base import Reader, ReaderError
from app.extraction.readers.registry import ReaderRegistry, build_registry

logger = logging.getLogger(__name__)

SOURCE_TYPE = "file"
HASH_BLOCK_SIZE = 1 << 20  # read the file in 1 MiB blocks, so a large PDF is never held in memory


def hash_file(path: Path) -> str:
    """SHA-256 of a file's raw bytes: the cheap signal for "has this file changed at all".

    Cheap is the point — hashing a whole corpus costs milliseconds where parsing it costs
    seconds. mtime and size would be cheaper still and are not used: cloud sync, `cp -p` and
    rsync without checksums all preserve mtime while the content changes, and a silently stale
    chunk is a wrong answer.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, kw_only=True)
class FileCandidate(SourceCandidate):
    """One file on disk: hashed and attributed to a reader, but not yet parsed."""

    path: Path
    parser: Reader
    limits: FileLimits = field(default_factory=FileLimits)
    # Set when enumeration already knows the file must not be opened (a symlink out of the
    # corpus, a file over the size cap). Reported when the run reaches it, like any other
    # failure, so the run carries on.
    rejection: LimitExceededError | None = None

    def read(self) -> ExtractedDocument:
        try:
            if self.rejection is not None:
                raise self.rejection
            check_size(self.path, self.limits)
            check_archive(self.path, self.limits)
            result = self.parser.read(self.path)
        except LimitExceededError as exc:
            raise LimitExceededError(exc.reason, f"{self.source_uri}: {exc.detail}") from exc
        except (OSError, UnicodeDecodeError, ReaderError) as exc:
            raise ExtractionError(f"{self.source_uri}: {exc}") from exc
        except Exception as exc:  # a bug in one reader must not stop the whole run
            logger.exception("the %s reader failed on %s", self.parser.name, self.path)
            raise ExtractionError(
                f"{self.source_uri}: the {self.parser.name} reader failed"
            ) from exc
        metadata: JsonObject = {
            "filename": self.path.name,
            "extension": self.path.suffix.lower(),
            "reader": self.parser.name,
            "reader_version": self.parser.version,
            **result.metadata,
        }
        if not result.text.strip():
            logger.warning("%s: the %s reader found no text", self.path.name, self.parser.name)
        return ExtractedDocument(
            source_type=self.source_type,
            source_uri=self.source_uri,
            text=result.text,
            metadata=metadata,
            elements=result.elements,
        )


class FilesystemExtractor:
    """Reads every file under a directory with a wanted extension.

    `extensions` lists the wanted ones; None means every extension that has a reader, so a file
    type nothing can describe (an image, an archive) is left alone rather than read as text.

    The extractor decides which files to visit; the reader for each one decides how to describe
    it. Enumeration parses nothing: it hashes each file's bytes and names the reader that would
    handle it, which is all a run needs to tell an unchanged file from one worth opening.
    """

    def __init__(
        self,
        root: Path,
        extensions: Sequence[str] | None = None,
        pdf_options: PdfOptions | None = None,
        registry: ReaderRegistry | None = None,
        extra_readers: Sequence[Reader] = (),
        limits: FileLimits | None = None,
    ) -> None:
        self._root = root
        self._limits = limits or FileLimits()
        self._registry = registry or build_registry(pdf_options, extra_readers, self._limits)
        wanted = self._registry.extensions() if extensions is None else extensions
        self._extensions = frozenset(e.lower() for e in wanted)

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE

    def candidates(self) -> Iterator[FileCandidate]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"extraction path is not a directory: {self._root}")
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            reader = self._registry.for_path(path)
            rejection = self._reject(path)
            yield FileCandidate(
                source_type=SOURCE_TYPE,
                source_uri=path.relative_to(self._root).as_posix(),
                # A rejected file is never read, not even to hash it: "" forces the open that
                # reports the rejection.
                source_hash="" if rejection is not None else self._hash(path),
                reader=reader.name,
                reader_version=reader.version,
                path=path,
                parser=reader,
                limits=self._limits,
                rejection=rejection,
            )

    def _reject(self, path: Path) -> LimitExceededError | None:
        """Decided from the directory entry alone, before a byte of the file is read."""
        if outside_root(path, self._root):
            return LimitExceededError(
                "outside_corpus", "a symlink that resolves outside the extraction path"
            )
        try:
            check_size(path, self._limits)
        except LimitExceededError as exc:
            return exc
        except OSError:
            return None  # hashing reports it
        return None

    def _hash(self, path: Path) -> str:
        """The file's raw hash, or "" if it cannot be read — which forces an open, and so a
        reported failure, rather than a file quietly treated as unchanged or as deleted."""
        try:
            return hash_file(path)
        except OSError as exc:
            logger.warning("could not hash %s (%s); it will be opened", path, exc)
            return ""
