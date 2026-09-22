"""Counting requests per API key per window, in Postgres.

The count has to be right when several workers are serving the same key at the same moment, so
it is done in one statement: an upsert that increments and returns the new value. Read-then-write
would let two workers both read 59 and both decide they were inside a limit of 60.
"""

import uuid
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ApiKeyRateLimit


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_request(self, api_key_id: uuid.UUID, window_start: datetime) -> int:
        """Record one request in this key's window and return the window's new total.

        Atomic: the increment happens inside the statement, so concurrent workers cannot both
        see the same pre-increment value.
        """
        statement = (
            insert(ApiKeyRateLimit)
            .values(api_key_id=api_key_id, window_start=window_start, request_count=1)
            .on_conflict_do_update(
                index_elements=[ApiKeyRateLimit.api_key_id, ApiKeyRateLimit.window_start],
                set_={"request_count": ApiKeyRateLimit.__table__.c.request_count + 1},
            )
            .returning(ApiKeyRateLimit.request_count)
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def purge_before(self, api_key_id: uuid.UUID, window_start: datetime) -> int:
        """Drop this key's finished windows, returning how many went.

        Called when a new window opens rather than on every request, so the table holds about one
        row per active key instead of growing for the life of the deployment.
        """
        result = await self._session.execute(
            delete(ApiKeyRateLimit)
            .where(
                ApiKeyRateLimit.api_key_id == api_key_id,
                ApiKeyRateLimit.window_start < window_start,
            )
            .returning(ApiKeyRateLimit.window_start)
        )
        return len(result.all())
