import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk


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
