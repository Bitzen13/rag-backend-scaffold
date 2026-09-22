"""Claiming and finishing ingestion jobs.

"One run at a time" is a database constraint, not a check in the application: two workers can
reach `claim` at the same instant, and only the partial unique index can decide between them.
`claim` therefore treats an IntegrityError as the answer "someone else is already running",
rather than looking first and acting afterwards, which is a race with a nice name.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import IngestionJob, JobState, JsonObject


class JobAlreadyRunningError(RuntimeError):
    """A run was requested while one was already in progress."""


class IngestionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self) -> IngestionJob:
        """Start a job, or raise `JobAlreadyRunningError` if another worker holds the slot."""
        job = IngestionJob(state=JobState.RUNNING.value, started_at=datetime.now(UTC))
        self._session.add(job)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise JobAlreadyRunningError("an ingestion run is already in progress") from exc
        return job

    async def finish(
        self,
        job_id: uuid.UUID,
        *,
        state: JobState,
        detail: str = "",
        report: JsonObject | None = None,
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(
                state=state.value,
                finished_at=datetime.now(UTC),
                detail=detail,
                report=report or {},
            )
        )

    async def get(self, job_id: uuid.UUID) -> IngestionJob | None:
        return await self._session.get(IngestionJob, job_id)

    async def recent(self, limit: int = 20) -> list[IngestionJob]:
        result = await self._session.execute(
            select(IngestionJob).order_by(IngestionJob.started_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def running(self) -> IngestionJob | None:
        result = await self._session.execute(
            select(IngestionJob).where(IngestionJob.state == JobState.RUNNING.value)
        )
        return result.scalar_one_or_none()

    async def abandon(self, job_id: uuid.UUID) -> bool:
        """Release a job whose worker died, so the next run can claim the slot.

        Only a running job can be abandoned; a finished state is final. Without this, one killed
        worker would block ingestion until someone edited the table by hand.
        """
        result = await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.state == JobState.RUNNING.value)
            .values(
                state=JobState.ABANDONED.value,
                finished_at=datetime.now(UTC),
                detail="released by an operator",
            )
            .returning(IngestionJob.id)
        )
        return result.first() is not None


def job_report(counts: str, opening: str, failures: Sequence[str]) -> JsonObject:
    """The summary stored on a finished job: what `scripts/ingest.py` would have printed."""
    return {"counts": counts, "opening": opening, "failures": list(failures)}
