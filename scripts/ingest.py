"""Ingest the configured source: extract, chunk, embed, and store.

Usage: python scripts/ingest.py [--path DIR]
       python scripts/ingest.py --dry-run [--path DIR] [--qa-json FILE]
       python scripts/ingest.py --verbose   # also log every chunk

--dry-run only extracts and chunks. It writes the chunks to chunked/ for inspection and prints a
quality report (sizes, oversized or empty chunks, duplicates, suspicious cuts); it needs no API
keys or database and stores nothing. --qa-json writes that report as JSON, which is how a
chunking baseline is recorded before changing chunking settings.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunking.base import ChunkingStrategyError
from chunking.factory import ConfiguredChunkers, build_chunkers
from chunking.qa import ChunkQaReport, format_report, format_summary, reports_as_json, review_chunks
from db.session import (
    create_engine,
    create_session_factory,
    database_url_from_settings,
)
from embedding.factory import build_embedding_provider
from extraction.base import read_all
from extraction.factory import build_extractor
from ingestion.pipeline import IngestionPipeline, IngestionReport, Outcome
from settings import ExtractionConfig, Settings, get_settings
from vector_store.pgvector_store import PgVectorStore

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNKED_DIR = REPO_ROOT / "chunked"
CHUNK_FILE_SUFFIX = ".chunks.txt"


def print_report(report: IngestionReport) -> None:
    for result in report.results:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"{result.outcome.value:>8}  {result.source_uri}{suffix}")
    print(f"\nDone: {report.counts()}.")
    print(report.opening_summary() + ".")


def dry_run(settings: Settings, extraction: ExtractionConfig, qa_json: Path | None) -> int:
    """Write each source's chunks to chunked/<source_uri>.chunks.txt and report their quality."""
    for stale in CHUNKED_DIR.rglob(f"*{CHUNK_FILE_SUFFIX}"):
        stale.unlink()

    chunkers: ConfiguredChunkers = build_chunkers(settings)
    reports: list[ChunkQaReport] = []
    missing = 0
    for document in read_all(build_extractor(extraction)):
        uri = document.source_uri
        try:
            plan = chunkers.plan_for(uri)
        except ChunkingStrategyError as exc:
            missing += 1
            print(f"  FAILED  {uri}: {exc}")
            continue
        chunks = chunkers.for_document(uri).chunk(document)
        report = review_chunks(uri, plan, document, chunks)
        reports.append(report)

        lines = [format_report(report, examples=10), "", f"source: {uri}"]
        for chunk in chunks:
            label = f"chunk {chunk.index} ({chunk.token_count} tokens)"
            details = json.dumps(chunk.metadata, ensure_ascii=False) if chunk.metadata else ""
            lines.append(f"\n===== {label} {details} =====\n{chunk.text}")
        target = CHUNKED_DIR / f"{uri}{CHUNK_FILE_SUFFIX}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(format_report(report))
        print(f"    written to {target.relative_to(REPO_ROOT).as_posix()}")

    print("\n" + format_summary(reports))
    if qa_json is not None:
        qa_json.parent.mkdir(parents=True, exist_ok=True)
        qa_json.write_text(
            json.dumps(reports_as_json(reports), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"quality report written to {qa_json}")
    print(f"Dry run: nothing stored ({CHUNKED_DIR.name}/ holds the chunks).")
    return 1 if missing else 0


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    extraction = settings.extraction
    if args.path is not None:
        extraction = extraction.model_copy(update={"path": args.path})
    if args.dry_run:
        return dry_run(settings, extraction, args.qa_json)
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(f"vector_db.provider {settings.vector_db.provider!r} is not supported yet")

    engine = create_engine(database_url_from_settings(settings))
    try:
        pipeline = IngestionPipeline(
            extractor=build_extractor(extraction),
            chunkers=build_chunkers(settings),
            embedder=build_embedding_provider(settings),
            session_factory=create_session_factory(engine),
            vector_store_factory=PgVectorStore,
        )
        report = await pipeline.run()
    finally:
        await engine.dispose()

    print_report(report)
    return 1 if report.of(Outcome.FAILED) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="override extraction.path from config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="write chunks to chunked/; store nothing"
    )
    parser.add_argument(
        "--qa-json", type=Path, help="with --dry-run, write the quality report as JSON to this file"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log every chunk (index, size, metadata, preview)"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per embedding request
    if args.verbose:
        logging.getLogger("ingestion").setLevel(logging.DEBUG)
    try:
        sys.exit(asyncio.run(main(args)))
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        sys.exit(f"error: {exc}")
