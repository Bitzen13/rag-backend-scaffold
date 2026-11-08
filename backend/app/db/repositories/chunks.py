import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ARRAY, Text, bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Source, SourceStatus


@dataclass(frozen=True)
class NewChunk:
    """A chunk to insert. The caller assigns `id`: it must equal the vector store chunk_id."""

    id: uuid.UUID
    source_id: uuid.UUID
    chunk_index: int
    text: str
    token_count: int
    embedding_model: str


class ChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many(self, chunks: Sequence[NewChunk]) -> list[Chunk]:
        rows = [
            Chunk(
                id=c.id,
                source_id=c.source_id,
                chunk_index=c.chunk_index,
                text=c.text,
                token_count=c.token_count,
                embedding_model=c.embedding_model,
            )
            for c in chunks
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def get_many(self, chunk_ids: Sequence[uuid.UUID]) -> list[Chunk]:
        """Return the chunks that exist, in the order of `chunk_ids` (missing ids are skipped)."""
        if not chunk_ids:
            return []
        result = await self._session.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
        by_id = {chunk.id: chunk for chunk in result.scalars()}
        return [by_id[i] for i in chunk_ids if i in by_id]

    async def get_many_with_sources(
        self, chunk_ids: Sequence[uuid.UUID]
    ) -> list[tuple[Chunk, Source]]:
        """Return (chunk, source) pairs for chunks of active sources, in the order of `chunk_ids`.

        Missing ids and chunks whose source is stale or deleted are skipped.
        """
        if not chunk_ids:
            return []
        result = await self._session.execute(
            select(Chunk, Source)
            .join(Source, Chunk.source_id == Source.id)
            .where(Chunk.id.in_(chunk_ids), Source.status == SourceStatus.ACTIVE.value)
        )
        by_id = {chunk.id: (chunk, source) for chunk, source in result.all()}
        return [by_id[i] for i in chunk_ids if i in by_id]

    async def list_by_source(self, source_id: uuid.UUID) -> Sequence[Chunk]:
        result = await self._session.execute(
            select(Chunk).where(Chunk.source_id == source_id).order_by(Chunk.chunk_index)
        )
        return result.scalars().all()

    async def delete_by_source(self, source_id: uuid.UUID) -> int:
        result = await self._session.execute(
            delete(Chunk).where(Chunk.source_id == source_id).returning(Chunk.id)
        )
        return len(result.all())

    async def fuzzy_search(
        self,
        phrases: Sequence[str],
        *,
        min_similarity: float,
        max_matches_per_phrase: int,
        limit: int,
        source_type: str | None = None,
    ) -> list["FuzzyMatch"]:
        """Find chunks containing text spelled like any of `phrases` (typo-tolerant).

        Uses pg_trgm `word_similarity`: a phrase matches a chunk when the phrase's letter
        trigrams closely match some stretch of the chunk text. A phrase that matches more than
        `max_matches_per_phrase` chunks is too common to be informative (a word like "the") and
        is ignored. Chunks are ranked by total evidence: each matching phrase adds its similarity
        times its length, so a chunk matching several words of the question outranks one matching
        a single word. Only chunks of active sources are returned, best first.
        """
        if not phrases or limit <= 0:
            return []
        # The `<%` operator compares against this threshold, and lets the GIN index serve it.
        await self._session.execute(
            text("SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)"),
            {"threshold": str(min_similarity)},
        )
        stmt = text(
            """
            WITH phrases AS (SELECT unnest(:phrases) AS p),
            hits AS (
                SELECT ph.p AS p, c.id AS chunk_id, word_similarity(ph.p, c.text) AS sim
                FROM phrases ph
                JOIN chunks c ON ph.p <% c.text
                JOIN sources s ON s.id = c.source_id
                WHERE s.status = 'active'
                  AND (CAST(:source_type AS text) IS NULL OR s.source_type = :source_type)
            ),
            usable AS (SELECT p FROM hits GROUP BY p HAVING count(*) <= :max_matches)
            SELECT h.chunk_id,
                   max(h.sim) AS sim,
                   sum(h.sim * length(replace(h.p, ' ', ''))) AS evidence,
                   (array_agg(h.p ORDER BY h.sim * length(h.p) DESC))[1] AS phrase
            FROM hits h JOIN usable u ON u.p = h.p
            GROUP BY h.chunk_id
            ORDER BY evidence DESC, h.chunk_id
            LIMIT :limit
            """
        ).bindparams(bindparam("phrases", type_=ARRAY(Text)))
        result = await self._session.execute(
            stmt,
            {
                "phrases": list(phrases),
                "source_type": source_type,
                "max_matches": max_matches_per_phrase,
                "limit": limit,
            },
        )
        return [
            FuzzyMatch(
                chunk_id=row.chunk_id,
                similarity=float(row.sim),
                evidence=float(row.evidence),
                phrase=row.phrase,
            )
            for row in result
        ]


@dataclass(frozen=True)
class FuzzyMatch:
    chunk_id: uuid.UUID
    similarity: float  # best pg_trgm word_similarity in [0, 1]; 1.0 = phrase appears verbatim
    evidence: float  # total match strength across all question phrases (longer matches count more)
    phrase: str  # the question phrase that matched best
