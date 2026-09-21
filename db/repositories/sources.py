import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import JsonObject, Source, SourceStatus


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        source_type: str,
        source_uri: str,
        content_hash: str,
        metadata: JsonObject | None = None,
        chunking: JsonObject | None = None,
    ) -> Source:
        source = Source(
            source_type=source_type,
            source_uri=source_uri,
            content_hash=content_hash,
            last_ingested_at=datetime.now(UTC),
            status=SourceStatus.ACTIVE.value,
            metadata_=metadata or {},
            chunking=chunking or {},
        )
        self._session.add(source)
        await self._session.flush()
        return source

    async def get(self, source_id: uuid.UUID) -> Source | None:
        return await self._session.get(Source, source_id)

    async def get_by_uri(self, source_type: str, source_uri: str) -> Source | None:
        result = await self._session.execute(
            select(Source).where(Source.source_type == source_type, Source.source_uri == source_uri)
        )
        return result.scalar_one_or_none()

    async def list(self, status: SourceStatus | None = None) -> Sequence[Source]:
        stmt = select(Source).order_by(Source.source_type, Source.source_uri)
        if status is not None:
            stmt = stmt.where(Source.status == status.value)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def mark_ingested(
        self,
        source: Source,
        *,
        content_hash: str,
        metadata: JsonObject | None = None,
        chunking: JsonObject | None = None,
    ) -> Source:
        """Record a completed (re-)ingestion: new hash, fresh timestamp, status back to active."""
        source.content_hash = content_hash
        source.last_ingested_at = datetime.now(UTC)
        source.status = SourceStatus.ACTIVE.value
        if metadata is not None:
            source.metadata_ = metadata
        if chunking is not None:
            source.chunking = chunking
        await self._session.flush()
        return source

    async def set_status(self, source: Source, status: SourceStatus) -> Source:
        source.status = status.value
        await self._session.flush()
        return source
