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

--adversarial runs the attack set (evaluation/adversarial/) against the real model: it ingests the
attack documents into the configured database, asks each attack's question, and reports whether
each attack was retrieved and whether it succeeded. It needs both API keys and costs real calls.
It refuses to run unless the database name ends in `_adversarial`, because ingestion deletes
every source not in its input and a misdirected run would wipe a real corpus:

  DATABASE_URL=postgresql://rag:rag@localhost:5432/rag_adversarial alembic upgrade head
  DATABASE_URL=postgresql://rag:rag@localhost:5432/rag_adversarial \\
      python scripts/evaluate.py --adversarial
"""

import argparse
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import customisations
from chunking.factory import build_chunkers
from db.repositories.chunks import ChunkRepository
from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingError
from embedding.factory import build_embedding_provider
from evaluation import attacks as attack_set
from evaluation import baseline as baselines
from evaluation.check import check_dataset
from evaluation.dataset import DatasetError, load_dataset
from evaluation.harness import EvalRun, RetrievalHarness, format_run
from extraction.factory import build_extractor
from generation.base import LLMError
from generation.factory import build_llm_provider
from ingestion.pipeline import Outcome
from observability import configure_logging, install_handler
from retrieval.retriever import Retriever
from safety.output import OutputPolicy, OutputSanitizer
from services.factory import build_query_service
from services.ingestion import build_ingestion_pipeline
from settings import DocumentChunking, Settings, get_settings
from vector_store.base import VectorFilter
from vector_store.pgvector_store import PgVectorStore

DEFAULT_DATASET = Path("evaluation/questions.yaml")


async def evaluate(dataset: Path, source_type: str | None) -> EvalRun:
    settings = get_settings()
    configure_logging(settings)
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


def _attack_settings(settings: Settings, corpus: Path, staged: list[attack_set.Attack]) -> Settings:
    """This deployment's settings, pointed at the staged attack documents and nothing else."""
    extraction = settings.extraction.model_copy(
        update={
            "provider": "filesystem",
            "path": corpus,
            "extensions": tuple(sorted({Path(a.uri).suffix.lower() for a in staged})),
        }
    )
    chunking = settings.chunking.model_copy(
        update={"documents": {a.uri: DocumentChunking(strategy="document_aware") for a in staged}}
    )
    return settings.model_copy(
        update={"extraction": extraction, "chunking": chunking, "customisation": None}
    )


async def adversarial() -> attack_set.AttackRun:
    """Ingest the attack set into a dedicated database and ask the real model each question."""
    settings = get_settings()
    configure_logging(settings)
    url = database_url_from_settings(settings)
    attack_set.require_attack_database(make_url(url).database)
    attacks = attack_set.load_attacks()
    output = OutputSanitizer(OutputPolicy.of(settings.safety.output.allowed_link_hosts))
    run = attack_set.AttackRun()

    with tempfile.TemporaryDirectory() as directory:
        skipped = attack_set.stage_corpus(attacks, Path(directory))
        staged = [a for a in attacks if a.id not in skipped]
        run_settings = _attack_settings(settings, Path(directory), staged)
        engine = create_engine(url, settings.database)
        try:
            sessions = create_session_factory(engine)
            # The database holds the attack set and nothing else, so emptying it is intended.
            report = await build_ingestion_pipeline(run_settings, sessions).run(
                allow_mass_delete=True
            )
            for failure in report.of(Outcome.FAILED):
                print(f"warning: {failure.source_uri} was not ingested: {failure.detail}")
            embedder = build_embedding_provider(settings)
            llm = attack_set.RecordingLLM(build_llm_provider(settings))
            for attack in attacks:
                if attack.id in skipped:
                    run.results.append(
                        attack_set.AttackResult(attack, False, skipped=skipped[attack.id])
                    )
                    continue
                async with sessions() as session:
                    service = build_query_service(run_settings, session, embedder=embedder, llm=llm)
                    run.results.append(
                        await attack_set.run_attack(
                            attack,
                            service,
                            llm,
                            retrieved_uri=output.clean(attack.uri).text,
                            filters=VectorFilter(source_type="file"),
                        )
                    )
        finally:
            await engine.dispose()
    return run


def check(dataset: Path) -> int:
    """Report the cases the corpus cannot satisfy, without touching the database."""
    settings = get_settings()
    result = check_dataset(
        load_dataset(dataset),
        build_extractor(settings.extraction, customisations.load(settings.customisation)),
        build_chunkers(settings),
    )
    print(result.describe())
    return 0 if result.ok else 1


def main(args: argparse.Namespace) -> int:
    if args.check_dataset:
        return check(args.dataset)
    if args.adversarial:
        attack_run = asyncio.run(adversarial())
        print(attack_set.format_run(attack_run))
        deterministic = ("exfiltration", "unsafe_url", "prompt_breakout")
        return 1 if any(attack_run.succeeded(goal) for goal in deterministic) else 0
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
    parser.add_argument(
        "--adversarial",
        action="store_true",
        help="run the attack set against the real model (database name must end in _adversarial)",
    )
    parser.add_argument("--json", type=Path, help="write the per-question results here")
    parser.add_argument("--save-baseline", type=Path, help="record this run as the baseline")
    parser.add_argument(
        "--baseline", type=Path, help="compare against this baseline; fail on a regression"
    )
    parser.add_argument("--label", default="", help="a note stored with a recorded baseline")
    parser.add_argument("--verbose", action="store_true", help="log every retrieval call")
    args = parser.parse_args()
    install_handler(level=logging.WARNING, text_format="%(message)s", stream=sys.stderr)
    if args.verbose:
        for name in ("retrieval", "evaluation"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        sys.exit(main(args))
    except (
        RuntimeError,
        EmbeddingError,
        LLMError,
        DatasetError,
        baselines.BaselineError,
        attack_set.AttackSetError,
    ) as exc:
        sys.exit(f"error: {exc}")
