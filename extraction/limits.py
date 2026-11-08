"""What one file may cost to read, checked before the expensive part.

A small file can be very expensive to process: an XLSX of 4.8 KB can declare a 4,000 x 4,000 grid
(135 MB of memory to iterate), and a zip archive can declare gigabytes of content. These checks
look at what the file *declares* — its size on disk, the uncompressed sizes in its zip directory,
its sheets' dimensions — so a hostile file is rejected before anything is unpacked.

A rejected file raises `LimitExceededError`, an `ExtractionError`, so the run reports it as failed
with the named reason and carries on with the rest (context/safety-plan.md §4).
"""

import zipfile
from dataclasses import dataclass
from pathlib import Path

from extraction.base import ExtractionError

# Formats that are zip archives, whose declared uncompressed size is checked before opening.
ARCHIVE_EXTENSIONS = frozenset({".docx", ".docm", ".xlsx", ".xlsm", ".xltx"})


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 500 * 1024 * 1024
    max_spreadsheet_cells: int = 2_000_000


class LimitExceededError(ExtractionError):
    """A source was rejected by a limit. `reason` is a stable name for which one."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"rejected ({reason}): {message}")
        self.reason = reason
        self.detail = message


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def outside_root(path: Path, root: Path) -> bool:
    """True for a path (normally a symlink) that resolves to somewhere outside the corpus."""
    try:
        return not path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):  # a broken or looping link
        return True


def check_size(path: Path, limits: FileLimits) -> None:
    size = path.stat().st_size
    if size > limits.max_file_bytes:
        raise LimitExceededError(
            "file_too_large",
            f"{_mb(size)} exceeds extraction.limits.max_file_bytes ({_mb(limits.max_file_bytes)})",
        )


def check_archive(path: Path, limits: FileLimits) -> None:
    """Reject a zip-based document whose entries declare more than the cap when unpacked."""
    if path.suffix.lower() not in ARCHIVE_EXTENSIONS:
        return
    try:
        with zipfile.ZipFile(path) as archive:
            declared = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError):
        return  # not a valid archive: the reader reports that, with its own message
    if declared > limits.max_archive_uncompressed_bytes:
        raise LimitExceededError(
            "archive_too_large",
            f"the archive declares {_mb(declared)} uncompressed, over "
            f"extraction.limits.max_archive_uncompressed_bytes "
            f"({_mb(limits.max_archive_uncompressed_bytes)})",
        )


def check_spreadsheet_cells(rows: int, columns: int, limit: int, *, sheet: str) -> None:
    cells = rows * columns
    if cells > limit:
        raise LimitExceededError(
            "spreadsheet_too_large",
            f"sheet {sheet!r} declares {rows:,} x {columns:,} = {cells:,} cells, over "
            f"extraction.limits.max_spreadsheet_cells ({limit:,})",
        )
