import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JsonObject, Source, SourceStatus


@dataclass(frozen=True)
class SourceState:
    """What a run needs to know about a stored source before deciding to open its file.

    Loaded for a whole source type in one query, so change detection costs one round trip for
    the run rather than one per document.
    """

    id: uuid.UUID
    source_uri: str
    source_hash: str  # of the raw bytes, as recorded at the last ingestion
    content_hash: str  # of the extracted text
    status: str
    chunking: JsonObject  # the plan (and reader) this source was last ingested with


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        source_type: str,
        source_uri: str,
        content_hash: str,
        source_hash: str = "",
        metadata: JsonObject | None = None,
        chunking: JsonObject | None = None,
    ) -> Source:
        source = Source(
            source_type=source_type,
            source_uri=source_uri,
            source_hash=source_hash,
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

    async def state_for_type(self, source_type: str) -> dict[str, SourceState]:
        """Every stored source of one type, keyed by `source_uri`, in a single query."""
        result = await self._session.execute(
            select(
                Source.id,
                Source.source_uri,
                Source.source_hash,
                Source.content_hash,
                Source.status,
                Source.chunking,
            ).where(Source.source_type == source_type)
        )
        return {
            row.source_uri: SourceState(
                id=row.id,
                source_uri=row.source_uri,
                source_hash=row.source_hash,
                content_hash=row.content_hash,
                status=row.status,
                chunking=row.chunking,
            )
            for row in result.all()
        }

    async def mark_ingested(
        self,
        source: Source,
        *,
        content_hash: str,
        source_hash: str | None = None,
        metadata: JsonObject | None = None,
        chunking: JsonObject | None = None,
    ) -> Source:
        """Record a completed (re-)ingestion: new hashes, fresh timestamp, status back to active."""
        source.content_hash = content_hash
        source.last_ingested_at = datetime.now(UTC)
        source.status = SourceStatus.ACTIVE.value
        if source_hash is not None:
            source.source_hash = source_hash
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
