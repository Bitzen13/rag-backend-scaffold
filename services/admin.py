"""What the internal admin routes actually do, kept out of the route handlers.

Two small services rather than repositories reached for inside a handler. The reason is the one
in code-standards.md — thin handlers, data access behind a contract — plus a practical one: a
handler that builds its own repository cannot be given a different one, so the admin surface
could only ever be tested against a live database. The routes that decide who may do these
things are worth testing far more often than that would allow.

Each method commits its own work, because an operator action is complete when it is done: a key
that was issued must survive whatever the rest of the request does.
"""

import uuid
from typing import Protocol

from db.models import ApiKey, IngestionJob
from services.api_keys import ApiKeyService, ApiKeyStore
from services.usage import Committer


class ApiKeyAdminStore(ApiKeyStore, Protocol):
    """Everything key management needs; `ApiKeyRepository` is the implementation."""

    async def list_all(self) -> list[ApiKey]: ...

    async def revoke(self, api_key_id: uuid.UUID) -> bool: ...


class JobStore(Protocol):
    """Everything job inspection needs; `IngestionJobRepository` is the implementation."""

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None: ...

    async def recent(self, limit: int = 20) -> list[IngestionJob]: ...

    async def abandon(self, job_id: uuid.UUID) -> bool: ...


class KeyAdminService:
    def __init__(self, keys: ApiKeyAdminStore, session: Committer) -> None:
        self._keys = keys
        self._session = session

    async def issue(self, label: str) -> tuple[str, ApiKey]:
        """Issue a key, returning its plaintext — the only moment one exists — and its row."""
        plaintext, row = await ApiKeyService(self._keys).issue(label)
        await self._session.commit()
        return plaintext, row

    async def list(self) -> list[ApiKey]:
        return await self._keys.list_all()

    async def revoke(self, api_key_id: uuid.UUID) -> ApiKey | None:
        """Deactivate a key and return its row, or None if there is no such key."""
        if not await self._keys.revoke(api_key_id):
            return None
        await self._session.commit()
        return next((k for k in await self._keys.list_all() if k.id == api_key_id), None)


class JobAdminService:
    def __init__(self, jobs: JobStore, session: Committer) -> None:
        self._jobs = jobs
        self._session = session

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        return await self._jobs.get(job_id)

    async def recent(self) -> list[IngestionJob]:
        return await self._jobs.recent()

    async def abandon(self, job_id: uuid.UUID) -> IngestionJob | None:
        """Release a run believed dead, returning its row, or None if it was not running."""
        if not await self._jobs.abandon(job_id):
            return None
        await self._session.commit()
        return await self._jobs.get(job_id)
