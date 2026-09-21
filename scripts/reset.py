"""Reset ingested data to a clean slate, so the next run re-ingests instead of skipping.

Usage: python scripts/reset.py                        # every source, after confirmation
       python scripts/reset.py --source notes.md      # one document (repeatable)
       python scripts/reset.py --dry-run              # list what would be deleted, delete nothing
       python scripts/reset.py --yes                  # skip the confirmation prompt
       python scripts/reset.py --keep-chunked         # leave the chunked/ dry-run output in place

This is for starting from scratch, not for re-chunking: ingestion already re-chunks a document
whose settings under `chunking.documents` changed, or whose reader's version was bumped. Deleting
a source cascades to its `chunks` and `chunk_vectors` in one transaction, which is what keeps
Postgres and the vector store consistent. The chunk files written
by `ingest.py --dry-run` go too, since they describe the chunking that was just discarded. API keys
and query logs are never touched, and the schema itself is left alone — no migration needs
re-running afterwards.
"""

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk, ChunkVector, Source
from db.session import create_engine, create_session_factory, database_url_from_settings
from settings import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNKED_DIR = REPO_ROOT / "chunked"
CHUNK_FILE_SUFFIX = ".chunks.txt"  # what ingest.py --dry-run writes; nothing else is ever removed


@dataclass(frozen=True)
class SourceSummary:
    """One stored source and how much of the index it owns."""

    source_uri: str
    source_type: str
    status: str
    chunks: int
    vectors: int


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


async def summarize(session: AsyncSession, uris: list[str]) -> list[SourceSummary]:
    """Every stored source, or only those named, with its chunk and vector counts."""
    chunks = (
        select(func.count())
        .select_from(Chunk)
        .where(Chunk.source_id == Source.id)
        .scalar_subquery()
    )
    vectors = (
        select(func.count())
        .select_from(ChunkVector)
        .where(ChunkVector.source_id == Source.id)
        .scalar_subquery()
    )
    statement = select(
        Source.source_uri, Source.source_type, Source.status, chunks, vectors
    ).order_by(Source.source_uri)
    if uris:
        statement = statement.where(Source.source_uri.in_(uris))
    result = await session.execute(statement)
    return [SourceSummary(*row) for row in result.all()]


async def count_orphans(session: AsyncSession) -> tuple[int, int]:
    """Chunk and vector rows left behind — both must be 0 after a full reset."""
    chunks = await session.scalar(select(func.count()).select_from(Chunk))
    vectors = await session.scalar(select(func.count()).select_from(ChunkVector))
    return chunks or 0, vectors or 0


def chunk_files(uris: list[str]) -> list[Path]:
    """The chunked/ files in scope: one per named source, or every one written there."""
    if not CHUNKED_DIR.is_dir():
        return []
    if uris:
        candidates = [CHUNKED_DIR / f"{uri}{CHUNK_FILE_SUFFIX}" for uri in uris]
        return [path for path in candidates if path.is_file()]
    return sorted(CHUNKED_DIR.rglob(f"*{CHUNK_FILE_SUFFIX}"))


def delete_chunk_files(paths: list[Path]) -> int:
    """Remove the given chunk files, then prune the directories they emptied."""
    for path in paths:
        path.unlink(missing_ok=True)
    for directory in sorted(CHUNKED_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return len(paths)


def print_summary(summaries: list[SourceSummary], files: list[Path]) -> None:
    if summaries:
        width = max(len(s.source_uri) for s in summaries)
        for summary in summaries:
            print(
                f"  {summary.source_uri:<{width}}  {summary.chunks:>6} chunks  "
                f"{summary.vectors:>6} vectors  ({summary.status})"
            )
        print(
            f"\n{plural(len(summaries), 'source')}, {sum(s.chunks for s in summaries)} chunks, "
            f"{sum(s.vectors for s in summaries)} vectors."
        )
    if files:
        print(f"\n{plural(len(files), 'chunk file')} under {CHUNKED_DIR.name}/:")
        for path in files:
            print(f"  {path.relative_to(REPO_ROOT).as_posix()}")


def confirm(summaries: list[SourceSummary], files: list[Path]) -> bool:
    """Ask before deleting. Re-embedding what this removes costs another run of the API bill."""
    if not sys.stdin.isatty():
        print("error: not a terminal; re-run with --yes to confirm non-interactively")
        return False
    if not summaries:
        scope = plural(len(files), "chunk file")
    elif len(summaries) > 1:
        scope = "ALL ingested data"
    else:
        scope = f"'{summaries[0].source_uri}'"
    answer = input(f"\nDelete {scope}? This cannot be undone. Type 'yes' to confirm: ")
    return answer.strip().lower() == "yes"


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(
            f"vector_db.provider {settings.vector_db.provider!r} is not supported yet: "
            "this script clears the pgvector tables only"
        )

    files = [] if args.keep_chunked else chunk_files(args.source)
    engine = create_engine(database_url_from_settings(settings))
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            summaries = await summarize(session, args.source)

        if not summaries and not files:
            if args.source:
                print(f"Nothing stored or chunked for: {', '.join(args.source)}")
                return 1
            print("Nothing to reset: no sources are stored.")
            return 0

        missing = sorted(set(args.source) - {s.source_uri for s in summaries})
        if missing:
            print(f"warning: not stored, ignored: {', '.join(missing)}\n")

        print("Would delete:" if args.dry_run else "Will delete:")
        print_summary(summaries, files)

        if args.dry_run:
            print("\nDry run: nothing deleted.")
            return 0
        if not args.yes and not confirm(summaries, files):
            print("Cancelled; nothing deleted.")
            return 1

        # One transaction: sources go, and their chunks and vectors go with them (ON DELETE
        # CASCADE), so the index can never be left half-deleted.
        async with session_factory() as session, session.begin():
            if summaries:
                statement = delete(Source)
                if args.source:
                    statement = statement.where(Source.source_uri.in_(args.source))
                await session.execute(statement)

        async with session_factory() as session:
            remaining_chunks, remaining_vectors = await count_orphans(session)
    finally:
        await engine.dispose()

    removed_files = delete_chunk_files(files)
    print()
    if summaries:
        print(f"Deleted {plural(len(summaries), 'source')} with their chunks and vectors.")
    if removed_files:
        print(f"Deleted {plural(removed_files, 'chunk file')} from {CHUNKED_DIR.name}/.")
    if args.source:
        print(f"{remaining_chunks} chunks and {remaining_vectors} vectors remain (other sources).")
    elif remaining_chunks or remaining_vectors:
        print(f"error: {remaining_chunks} chunks and {remaining_vectors} vectors were left behind")
        return 1
    else:
        print("Clean slate: no chunks and no vectors remain.")
    print("Re-run `python scripts/ingest.py` to ingest from scratch.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="URI",
        help="reset only this source_uri (repeatable); default is every source",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be deleted, delete nothing"
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--keep-chunked",
        action="store_true",
        help=f"leave the {CHUNKED_DIR.name}/ dry-run output in place",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(main(args)))
    except (RuntimeError, ValueError) as exc:
        sys.exit(f"error: {exc}")
