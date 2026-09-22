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

from db.session import create_engine, create_session_factory, database_url_from_settings
from embedding.base import EmbeddingError
from embedding.factory import build_embedding_provider
from generation.base import LLMError
from generation.factory import build_llm_provider
from services.factory import build_query_service
from settings import get_settings
from vector_store.base import VectorFilter


async def main(question: str, source_type: str | None) -> None:
    settings = get_settings()
    embedder = build_embedding_provider(settings)
    llm = build_llm_provider(settings)
    engine = create_engine(database_url_from_settings(settings))
    try:
        async with create_session_factory(engine)() as session:
            # The same wiring /api/query uses, so the two answer a question identically.
            service = build_query_service(settings, session, embedder=embedder, llm=llm)
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
