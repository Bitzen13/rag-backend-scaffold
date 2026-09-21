"""Measure retrieval quality against an evaluation set (no LLM involved).

Usage: python scripts/evaluate.py [--dataset FILE] [--source-type file] [--verbose]
       python scripts/evaluate.py --save-baseline evaluation/baselines/retrieval.json
       python scripts/evaluate.py --baseline evaluation/baselines/retrieval.json
       python scripts/evaluate.py --check-dataset    # needs no database or API key

Reports recall@k and MRR over the questions in the evaluation set, using the same retrieval path
the API uses. Needs the data ingested, a database, and an embedding API key.

Record a baseline before changing anything about chunking or retrieval, then compare against it
afterwards: with --baseline the script exits non-zero if a metric regressed, so it can gate a
change. --json writes the full per-question result.

--check-dataset instead re-chunks the corpus and reports the cases no chunk can satisfy, which
tells a typo in the evaluation set apart from a retrieval failure.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunking.factory import build_chunkers
from db.repositories.chunks import ChunkRepository
from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingError
from embedding.factory import build_embedding_provider
from evaluation import baseline as baselines
from evaluation.check import check_dataset
from evaluation.dataset import DatasetError, load_dataset
from evaluation.harness import EvalRun, RetrievalHarness, format_run
from extraction.factory import build_extractor
from retrieval.retriever import Retriever
from settings import get_settings
from vector_store.base import VectorFilter
from vector_store.pgvector_store import PgVectorStore

DEFAULT_DATASET = Path("evaluation/questions.yaml")


async def evaluate(dataset: Path, source_type: str | None) -> EvalRun:
    settings = get_settings()
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(f"vector_db.provider {settings.vector_db.provider!r} is not supported yet")

    cases = load_dataset(dataset)
    retrieval = settings.retrieval
    embedder = build_embedding_provider(settings)
    engine = create_engine(database_url_from_settings(settings))
    try:
        async with create_session_factory(engine)() as session:
            harness = RetrievalHarness(
                Retriever(
                    embedder=embedder,
                    vector_store=PgVectorStore(session),
                    chunks=ChunkRepository(session),
                    top_k=retrieval.top_k,
                    similarity_threshold=retrieval.similarity_threshold,
                    fuzzy_top_k=retrieval.fuzzy_top_k,
                    fuzzy_min_similarity=retrieval.fuzzy_min_similarity,
                    fuzzy_max_matches_per_phrase=retrieval.fuzzy_max_matches_per_phrase,
                    fuzzy_min_relative_evidence=retrieval.fuzzy_min_relative_evidence,
                ),
                top_k=retrieval.top_k,
            )
            return await harness.run(cases, VectorFilter(source_type=source_type))
    finally:
        await engine.dispose()


def check(dataset: Path) -> int:
    """Report the cases the corpus cannot satisfy, without touching the database."""
    settings = get_settings()
    result = check_dataset(
        load_dataset(dataset), build_extractor(settings.extraction), build_chunkers(settings)
    )
    print(result.describe())
    return 0 if result.ok else 1


def main(args: argparse.Namespace) -> int:
    if args.check_dataset:
        return check(args.dataset)
    run = asyncio.run(evaluate(args.dataset, args.source_type))
    print(format_run(run))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(run.as_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"per-question results written to {args.json}")

    if args.save_baseline is not None:
        recorded = baselines.from_run(run, dataset=args.dataset.as_posix(), label=args.label)
        baselines.save(recorded, args.save_baseline)
        print(f"baseline recorded in {args.save_baseline}")

    if args.baseline is not None:
        comparison = baselines.compare(baselines.load(args.baseline), run)
        print(comparison.describe())
        return 1 if comparison.regressed else 0
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=DEFAULT_DATASET, help="evaluation set (YAML)"
    )
    parser.add_argument("--source-type", help="only search sources of this type")
    parser.add_argument(
        "--check-dataset",
        action="store_true",
        help="check the cases against freshly chunked documents instead of running retrieval",
    )
    parser.add_argument("--json", type=Path, help="write the per-question results here")
    parser.add_argument("--save-baseline", type=Path, help="record this run as the baseline")
    parser.add_argument(
        "--baseline", type=Path, help="compare against this baseline; fail on a regression"
    )
    parser.add_argument("--label", default="", help="a note stored with a recorded baseline")
    parser.add_argument("--verbose", action="store_true", help="log every retrieval call")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    if args.verbose:
        for name in ("retrieval", "evaluation"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        sys.exit(main(args))
    except (RuntimeError, EmbeddingError, DatasetError, baselines.BaselineError) as exc:
        sys.exit(f"error: {exc}")
