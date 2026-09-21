"""Ask a question against the ingested data (no API involved).

Usage: python scripts/ask.py "your question" [--source-type file] [--verbose]

Prints only the answer. --verbose also logs (to stderr) every retrieved chunk with its similarity
score and whether it passed the threshold, plus the exact prompt sent to the LLM and its reply.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repositories.chunks import ChunkRepository
from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingError
from embedding.factory import build_embedding_provider
from generation.base import LLMError
from generation.factory import build_llm_provider
from retrieval.retriever import Retriever
from services.query_service import QueryService
from settings import get_settings
from vector_store.base import VectorFilter
from vector_store.pgvector_store import PgVectorStore


async def main(question: str, source_type: str | None) -> None:
    settings = get_settings()
    if settings.vector_db.provider != "pgvector":
        raise SystemExit(f"vector_db.provider {settings.vector_db.provider!r} is not supported yet")

    embedder = build_embedding_provider(settings)
    llm = build_llm_provider(settings)
    engine = create_engine(database_url_from_settings(settings))
    try:
        async with create_session_factory(engine)() as session:
            service = QueryService(
                retriever=Retriever(
                    embedder=embedder,
                    vector_store=PgVectorStore(session),
                    chunks=ChunkRepository(session),
                    top_k=settings.retrieval.top_k,
                    similarity_threshold=settings.retrieval.similarity_threshold,
                    fuzzy_top_k=settings.retrieval.fuzzy_top_k,
                    fuzzy_min_similarity=settings.retrieval.fuzzy_min_similarity,
                    fuzzy_max_matches_per_phrase=settings.retrieval.fuzzy_max_matches_per_phrase,
                    fuzzy_min_relative_evidence=settings.retrieval.fuzzy_min_relative_evidence,
                ),
                llm=llm,
                excerpt_max_chars=settings.retrieval.excerpt_max_chars,
            )
            result = await service.answer(question, VectorFilter(source_type=source_type))
    finally:
        await engine.dispose()

    print(result.answer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--source-type", help="only search sources of this type")
    parser.add_argument(
        "--verbose", action="store_true", help="log retrieved chunks, scores, and the LLM prompt"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    if args.verbose:
        for name in ("retrieval", "services"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        asyncio.run(main(args.question, args.source_type))
    except (RuntimeError, EmbeddingError, LLMError) as exc:
        sys.exit(f"error: {exc}")
