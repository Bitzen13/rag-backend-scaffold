import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey


class ApiKeyRepository:
    """Stores only key hashes; plaintext keys never reach this layer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, key_hash: str, label: str) -> ApiKey:
        api_key = ApiKey(key_hash=key_hash, label=label, is_active=True)
        self._session.add(api_key)
        await self._session.flush()
        return api_key

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Return the key row regardless of is_active; callers decide how to treat revoked keys."""
        result = await self._session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[ApiKey]:
        """Every key, active or revoked, oldest first. For the key-management CLI."""
        result = await self._session.execute(select(ApiKey).order_by(ApiKey.created_at))
        return list(result.scalars())

    async def revoke(self, api_key_id: uuid.UUID) -> bool:
        """Deactivate a key (rows are never deleted). Returns False if no such key exists."""
        result = await self._session.execute(
            update(ApiKey)
            .where(ApiKey.id == api_key_id)
            .values(is_active=False)
            .returning(ApiKey.id)
        )
        return result.first() is not None

    async def touch_last_used(self, api_key_id: uuid.UUID) -> None:
        await self._session.execute(
            update(ApiKey).where(ApiKey.id == api_key_id).values(last_used_at=datetime.now(UTC))
        )
