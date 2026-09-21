"""Ingest the configured source: extract, chunk, embed, and store.

Usage: python scripts/ingest.py [--path DIR]
       python scripts/ingest.py --dry-run [--path DIR]
       python scripts/ingest.py --verbose   # also log every chunk

--dry-run only extracts and chunks, writing the chunks to chunked/ for inspection. It needs no API
keys or database and stores nothing. Chunking settings come from config.yaml.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunking.base import ChunkingStrategyError
from chunking.factory import ConfiguredChunkers
from db.session import (
    create_engine,
    create_session_factory,
    database_url_from_settings,
)
from embedding.factory import build_embedding_provider
from extraction.factory import build_extractor
from ingestion.pipeline import IngestionPipeline, IngestionReport, Outcome
from settings import ChunkingConfig, ExtractionConfig, get_settings
from vector_store.pgvector_store import PgVectorStore

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNKED_DIR = REPO_ROOT / "chunked"
CHUNK_FILE_SUFFIX = ".chunks.txt"


def print_report(report: IngestionReport) -> None:
    for result in report.results:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"{result.outcome.value:>8}  {result.source_uri}{suffix}")
    counts = ", ".join(f"{len(report.of(o))} {o.value}" for o in Outcome)
    print(f"\nDone: {counts}.")


def dry_run(extraction: ExtractionConfig, chunking: ChunkingConfig) -> int:
    """Write each source's chunks to chunked/<source_uri>.chunks.txt (replacing earlier output)."""
    for stale in CHUNKED_DIR.rglob(f"*{CHUNK_FILE_SUFFIX}"):
        stale.unlink()

    chunkers = ConfiguredChunkers(chunking)
    total = 0
    missing = 0
    for document in build_extractor(extraction).extract():
        uri = document.source_uri
        try:
            strategy = chunkers.strategy_for(uri)
        except ChunkingStrategyError as exc:
            missing += 1
            print(f"  FAILED  {uri}: {exc}")
            continue
        chunks = chunkers.for_document(uri).chunk(document)
        total += len(chunks)
        parts = [
            f"chunking: {strategy}, size={chunking.chunk_size}, overlap={chunking.chunk_overlap}",
            f"source: {uri} ({len(chunks)} chunks)",
        ]
        for chunk in chunks:
            label = f"chunk {chunk.index} ({chunk.token_count} words)"
            details = json.dumps(chunk.metadata, ensure_ascii=False) if chunk.metadata else ""
            parts.append(f"\n===== {label} {details} =====\n{chunk.text}")
        target = CHUNKED_DIR / f"{uri}{CHUNK_FILE_SUFFIX}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(parts) + "\n", encoding="utf-8")
        print(f"{len(chunks):>6} chunks  [{strategy}]  {target.relative_to(REPO_ROOT)}")
    print(f"\nDry run: {total} chunk(s) written to {CHUNKED_DIR.name}/, nothing stored.")
    return 1 if missing else 0


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    extraction = settings.extraction
    if args.path is not None:
        extraction = extraction.model_copy(update={"path": args.path})
    if args.dry_run:
        return dry_run(extraction, settings.chunking)
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(f"vector_db.provider {settings.vector_db.provider!r} is not supported yet")

    engine = create_engine(database_url_from_settings(settings))
    try:
        pipeline = IngestionPipeline(
            extractor=build_extractor(extraction),
            chunkers=ConfiguredChunkers(settings.chunking),
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
