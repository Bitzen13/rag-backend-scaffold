"""Running ingestion in the background, on behalf of the internal admin route.

Ingestion takes minutes and belongs outside a request handler, so the route
claims a job, starts a task and answers 202. The job row is the shared truth about that run: it
is what the operator polls, and what stops a second run starting while the first is in flight.

What this deliberately is not: a general job queue. One operation, one at a time, per deployment.
A run whose worker is killed leaves its row `running` until an operator releases it — visible and
fixable, rather than silently lost or silently restarted on top of a run that may still be alive.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from app.db.models import IngestionJob, JobState
from app.db.repositories.jobs import IngestionJobRepository, job_report
from app.ingestion.pipeline import IngestionPipeline, Outcome, SessionFactory
from app.services.ingestion import build_ingestion_pipeline
from app.settings import Settings

logger = logging.getLogger(__name__)

# asyncio holds only a weak reference to a running task, so a task nobody keeps can be collected
# mid-flight. These references are dropped when the task finishes.
_running: set[asyncio.Task[None]] = set()

PipelineFactory = Callable[[], IngestionPipeline]


def spawn(coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Start a background task and keep it alive until it finishes."""
    task = asyncio.create_task(coroutine)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


class IngestionJobService:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        *,
        pipeline_factory: PipelineFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._pipeline_factory = pipeline_factory or (
            lambda: build_ingestion_pipeline(settings, session_factory)
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> "asyncio.Task[None] | None":
        """The background task the last `start` created, for anything that needs to wait on it."""
        return self._task

    async def start(self) -> IngestionJob:
        """Claim the run slot and start the work. Raises JobAlreadyRunningError if it is taken."""
        async with self._session_factory() as session, session.begin():
            job = await IngestionJobRepository(session).claim()
            job_id = job.id
        logger.info("ingestion job started", extra={"job_id": str(job_id)})
        self._task = spawn(self.run(job_id))
        return job

    async def run(self, job_id: uuid.UUID) -> None:
        """Run the pipeline and record how it went. Never raises: it has no caller to raise to."""
        state, detail, report = JobState.FAILED, "", None
        try:
            outcome = await self._pipeline_factory().run()
            failures = [f"{r.source_uri}: {r.detail}" for r in outcome.of(Outcome.FAILED)]
            report = job_report(outcome.counts(), outcome.opening_summary(), failures)
            state = JobState.FAILED if failures else JobState.SUCCEEDED
            detail = f"{len(failures)} source(s) failed" if failures else outcome.counts()
        except Exception as exc:  # a run must never leave its job row stuck at `running`
            logger.exception("ingestion job failed", extra={"job_id": str(job_id)})
            detail = f"{type(exc).__name__}: {exc}"
        await self._record(job_id, state, detail, report)

    async def _record(
        self, job_id: uuid.UUID, state: JobState, detail: str, report: dict[str, object] | None
    ) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await IngestionJobRepository(session).finish(
                    job_id, state=state, detail=detail, report=report
                )
        except Exception:
            # The run itself is over either way; say so loudly, because the row an operator polls
            # is now stale and only this log explains why.
            logger.exception("could not record the outcome of ingestion job %s", job_id)
        else:
            logger.info(
                "ingestion job finished", extra={"job_id": str(job_id), "state": state.value}
            )
