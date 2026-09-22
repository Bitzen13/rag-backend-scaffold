"""Wiring for the query path, shared by the API and scripts/ask.py so the two cannot drift."""

from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories.chunks import ChunkRepository
from embedding.base import EmbeddingProvider
from generation.base import LLMProvider
from retrieval.retriever import Retriever
from services.query_service import QueryService
from settings import Settings
from vector_store.pgvector_store import PgVectorStore


def build_query_service(
    settings: Settings,
    session: AsyncSession,
    *,
    embedder: EmbeddingProvider,
    llm: LLMProvider,
) -> QueryService:
    """A QueryService bound to one session. The caller owns the session's transaction."""
    if settings.vector_db.provider != "pgvector":
        raise RuntimeError(
            f"vector_db.provider {settings.vector_db.provider!r} is not supported yet"
        )
    retrieval = settings.retrieval
    return QueryService(
        retriever=Retriever(
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
        llm=llm,
        excerpt_max_chars=retrieval.excerpt_max_chars,
    )
