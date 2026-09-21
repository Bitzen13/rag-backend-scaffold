import logging
import uuid
from dataclasses import dataclass

from db.repositories.chunks import ChunkRepository, FuzzyMatch
from embedding.base import EmbeddingError, EmbeddingProvider
from retrieval.fuzzy import question_phrases
from vector_store.base import VectorFilter, VectorMatch, VectorStore

logger = logging.getLogger(__name__)

RRF_K = 60  # reciprocal-rank-fusion constant; the standard value


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    source_uri: str
    text: str
    score: float | None = None  # cosine similarity, when the vector search found it
    fuzzy_score: float | None = None  # trigram word similarity, when the fuzzy search found it


class Retriever:
    """Finds relevant chunks by meaning (vector search) and by spelling (fuzzy keyword search).

    The two result lists are merged with reciprocal rank fusion, so a chunk found by either
    method can make the cut, and one found by both ranks highest.
    """

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider,
        vector_store: VectorStore,
        chunks: ChunkRepository,
        top_k: int,
        similarity_threshold: float,
        fuzzy_top_k: int = 0,
        fuzzy_min_similarity: float = 0.6,
        fuzzy_max_matches_per_phrase: int = 50,
        fuzzy_min_relative_evidence: float = 0.5,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._chunks = chunks
        self._top_k = top_k
        self._threshold = similarity_threshold
        self._fuzzy_top_k = fuzzy_top_k
        self._fuzzy_min_similarity = fuzzy_min_similarity
        self._fuzzy_max_matches = fuzzy_max_matches_per_phrase
        self._fuzzy_min_relative = fuzzy_min_relative_evidence

    async def retrieve(
        self, question: str, filters: VectorFilter | None = None
    ) -> list[RetrievedChunk]:
        """Return up to top_k chunks, best first. Vector hits must reach the similarity threshold;
        fuzzy hits must reach the fuzzy similarity threshold."""
        vectors = await self._embedder.embed([question])
        if len(vectors) != 1:
            raise EmbeddingError(f"expected 1 embedding for the question, got {len(vectors)}")
        vector_matches = await self._vector_store.search(vectors[0], self._top_k, filters)
        fuzzy_matches = await self._fuzzy_search(question, filters)
        if logger.isEnabledFor(logging.DEBUG):
            await self._log_candidates(question, vector_matches, fuzzy_matches)

        passed = [m for m in vector_matches if m.score >= self._threshold]
        ranked = self._fuse([m.chunk_id for m in passed], [m.chunk_id for m in fuzzy_matches])
        if not ranked:
            return []

        vector_scores = {m.chunk_id: m.score for m in passed}
        fuzzy_scores = {m.chunk_id: m.similarity for m in fuzzy_matches}
        rows = await self._chunks.get_many_with_sources(ranked)
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                source_uri=source.source_uri,
                text=chunk.text,
                score=vector_scores.get(chunk.id),
                fuzzy_score=fuzzy_scores.get(chunk.id),
            )
            for chunk, source in rows
        ]

    async def _fuzzy_search(self, question: str, filters: VectorFilter | None) -> list[FuzzyMatch]:
        if self._fuzzy_top_k <= 0:
            return []
        hits = await self._chunks.fuzzy_search(
            question_phrases(question),
            min_similarity=self._fuzzy_min_similarity,
            max_matches_per_phrase=self._fuzzy_max_matches,
            limit=self._fuzzy_top_k,
            source_type=filters.source_type if filters is not None else None,
        )
        if not hits:
            return []
        # Keep only hits close in strength to the best one, so incidental matches on one stray
        # word do not ride along with a strong multi-word match.
        cutoff = hits[0].evidence * self._fuzzy_min_relative
        return [h for h in hits if h.evidence >= cutoff]

    def _fuse(self, vector_ids: list[uuid.UUID], fuzzy_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Reciprocal rank fusion of the two ranked lists, truncated to top_k."""
        scores: dict[uuid.UUID, float] = {}
        for ranked in (vector_ids, fuzzy_ids):
            for position, chunk_id in enumerate(ranked):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + position + 1)
        ordered = sorted(scores, key=lambda chunk_id: -scores[chunk_id])
        return ordered[: self._top_k]

    async def _log_candidates(
        self, question: str, matches: list[VectorMatch], fuzzy_matches: list[FuzzyMatch]
    ) -> None:
        """DEBUG: every candidate from both searches, with scores and whether it qualified."""
        logger.debug(
            "retrieval for %r: %d vector candidates (top_k=%d, similarity_threshold=%s)",
            question,
            len(matches),
            self._top_k,
            self._threshold,
        )
        # Read all candidates (including any below the threshold) so their text can be shown.
        wanted = [m.chunk_id for m in matches] + [m.chunk_id for m in fuzzy_matches]
        rows = await self._chunks.get_many_with_sources(wanted)
        by_id = {chunk.id: (chunk, source) for chunk, source in rows}

        def describe(chunk_id: uuid.UUID) -> tuple[str, str]:
            found = by_id.get(chunk_id)
            return (found[1].source_uri, found[0].text) if found else ("?", "(chunk not found)")

        for rank, match in enumerate(matches, start=1):
            verdict = "PASSED" if match.score >= self._threshold else "below threshold"
            uri, text = describe(match.chunk_id)
            logger.debug(
                "  vector #%d score=%.3f [%s] %s chunk_id=%s\n%s",
                rank,
                match.score,
                verdict,
                uri,
                match.chunk_id,
                text,
            )
        if self._fuzzy_top_k <= 0:
            return
        logger.debug(
            "fuzzy keyword search: %d hits (fuzzy_min_similarity=%s, top %d)",
            len(fuzzy_matches),
            self._fuzzy_min_similarity,
            self._fuzzy_top_k,
        )
        for rank, hit in enumerate(fuzzy_matches, start=1):
            uri, text = describe(hit.chunk_id)
            logger.debug(
                "  fuzzy #%d evidence=%.1f similarity=%.3f best phrase %r %s chunk_id=%s\n%s",
                rank,
                hit.evidence,
                hit.similarity,
                hit.phrase,
                uri,
                hit.chunk_id,
                text,
            )
