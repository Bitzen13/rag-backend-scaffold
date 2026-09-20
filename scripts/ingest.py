"""Ingest the configured source: extract, chunk, embed, and store.

Usage: python scripts/ingest.py [--path DIR]
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunking.factory import build_chunker
from db.session import (
    create_engine,
    create_session_factory,
    database_url_from_settings,
)
from embedding.factory import build_embedding_provider
from extraction.factory import build_extractor
from ingestion.pipeline import IngestionPipeline, IngestionReport, Outcome
from settings import get_settings
from vector_store.pgvector_store import PgVectorStore


def print_report(report: IngestionReport) -> None:
    for result in report.results:
        suffix = f" ({result.detail})" if result.detail else ""
        print(f"{result.outcome.value:>8}  {result.source_uri}{suffix}")
    counts = ", ".join(f"{len(report.of(o))} {o.value}" for o in Outcome)
    print(f"\nDone: {counts}.")


async def main(path: Path | None) -> int:
    settings = get_settings()
    extraction = settings.extraction
    if path is not None:
        extraction = extraction.model_copy(update={"path": path})
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(f"vector_db.provider {settings.vector_db.provider!r} is not supported yet")

    engine = create_engine(database_url_from_settings(settings))
    try:
        pipeline = IngestionPipeline(
            extractor=build_extractor(extraction),
            chunker=build_chunker(settings.chunking),
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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(asyncio.run(main(args.path)))
    except (RuntimeError, FileNotFoundError) as exc:
        sys.exit(f"error: {exc}")
