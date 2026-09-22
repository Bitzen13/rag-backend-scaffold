"""Writing the record of an answered question."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import QueryLog


class QueryLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        api_key_id: uuid.UUID,
        question: str,
        answer: str,
        retrieved_chunk_ids: Sequence[uuid.UUID],
    ) -> QueryLog:
        row = QueryLog(
            api_key_id=api_key_id,
            question=question,
            answer=answer,
            retrieved_chunk_ids=list(retrieved_chunk_ids),
        )
        self._session.add(row)
        await self._session.flush()
        return row
