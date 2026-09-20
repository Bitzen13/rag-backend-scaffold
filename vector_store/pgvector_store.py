import uuid
from collections.abc import Sequence

from sqlalchemy import Float, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EMBEDDING_DIMENSIONS, ChunkVector
from vector_store.base import VectorFilter, VectorMatch, VectorRecord


class PgVectorStore:
    """VectorStore backed by the chunk_vectors table.

    Bound to a session so vector writes share the transaction with the repositories, keeping
    Postgres rows and vectors consistent. Like the repositories, it flushes but never commits.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _check_dimensions(embedding: Sequence[float]) -> None:
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"embedding has {len(embedding)} dimensions; the vector column expects "
                f"{EMBEDDING_DIMENSIONS}"
            )

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        for record in records:
            self._check_dimensions(record.embedding)
        stmt = insert(ChunkVector).values(
            [
                {
                    "chunk_id": r.chunk_id,
                    "embedding": list(r.embedding),
                    "source_id": r.source_id,
                    "source_type": r.source_type,
                    "chunk_index": r.chunk_index,
                    "metadata_": r.metadata,
                }
                for r in records
            ]
        )
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChunkVector.chunk_id],
            set_={
                "embedding": excluded.embedding,
                "source_id": excluded.source_id,
                "source_type": excluded.source_type,
                "chunk_index": excluded.chunk_index,
                "metadata": excluded["metadata"],
            },
        )
        await self._session.execute(stmt)

    async def search(
        self, embedding: Sequence[float], top_k: int, filters: VectorFilter | None = None
    ) -> list[VectorMatch]:
        self._check_dimensions(embedding)
        distance = ChunkVector.embedding.op("<=>", return_type=Float)(list(embedding))
        stmt = (
            select(ChunkVector.chunk_id, distance.label("distance")).order_by(distance).limit(top_k)
        )
        if filters is not None and filters.source_type is not None:
            stmt = stmt.where(ChunkVector.source_type == filters.source_type)
        result = await self._session.execute(stmt)
        return [VectorMatch(chunk_id=row.chunk_id, score=1.0 - row.distance) for row in result]

    async def delete(self, chunk_ids: Sequence[uuid.UUID]) -> int:
        if not chunk_ids:
            return 0
        result = await self._session.execute(
            delete(ChunkVector)
            .where(ChunkVector.chunk_id.in_(chunk_ids))
            .returning(ChunkVector.chunk_id)
        )
        return len(result.all())

    async def delete_by_source(self, source_id: uuid.UUID) -> int:
        result = await self._session.execute(
            delete(ChunkVector)
            .where(ChunkVector.source_id == source_id)
            .returning(ChunkVector.chunk_id)
        )
        return len(result.all())
