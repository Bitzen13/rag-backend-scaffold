"""Ingest the configured source: extract, chunk, embed, and store.

Usage: python scripts/ingest.py [--path DIR] [--allow-mass-delete]
       python scripts/ingest.py --dry-run [--path DIR] [--qa-json FILE]
       python scripts/ingest.py --verbose   # also log every chunk

--dry-run only extracts and chunks. It writes the chunks to chunked/ for inspection and prints a
quality report (sizes, oversized or empty chunks, duplicates, suspicious cuts); it needs no API
keys or database and stores nothing. --qa-json writes that report as JSON, which is how a
chunking baseline is recorded before changing chunking settings.

A run that would delete more than extraction.limits.max_delete_fraction of the stored sources
stops before doing anything; --allow-mass-delete lets this one run go ahead.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customisations
from chunking.base import ChunkingStrategyError
from chunking.factory import ConfiguredChunkers, build_chunkers
from chunking.qa import ChunkQaReport, format_report, format_summary, reports_as_json, review_chunks
from db.session import (
    create_engine,
    create_session_factory,
    database_url_from_settings,
)
from extraction.base import read_all
from extraction.factory import build_extractor
from ingestion.pipeline import IngestionReport, Outcome
from observability import configure_logging, install_handler
from services.ingestion import build_ingestion_pipeline
from settings import ExtractionConfig, Settings, get_settings

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
    client = customisations.load(settings.customisation)
    for document in read_all(build_extractor(extraction, client)):
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
    configure_logging(settings)  # now that the secrets are known, redact them by value
    extraction = settings.extraction
    if args.path is not None:
        extraction = extraction.model_copy(update={"path": args.path})
    if args.dry_run:
        return dry_run(settings, extraction, args.qa_json)

    engine = create_engine(database_url_from_settings(settings))
    try:
        # The same pipeline the internal admin route runs, built the same way.
        pipeline = build_ingestion_pipeline(
            settings, create_session_factory(engine), extraction=extraction
        )
        report = await pipeline.run(allow_mass_delete=args.allow_mass_delete)
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
    parser.add_argument(
        "--allow-mass-delete",
        action="store_true",
        help="let this run delete more than extraction.limits.max_delete_fraction of the corpus",
    )
    args = parser.parse_args()
    install_handler(level=logging.INFO)  # replaced once settings load, with their secrets
    if args.verbose:
        logging.getLogger("ingestion").setLevel(logging.DEBUG)
    try:
        sys.exit(asyncio.run(main(args)))
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        sys.exit(f"error: {exc}")
