"""Background ingestion runs, against a real Postgres.

The database is not incidental here: "one run at a time" is a partial unique index, and a test
with an in-memory stand-in would prove nothing about the constraint that actually enforces it.
The pipeline itself is faked — this is about the job, not about chunking.
"""

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import JobState
from app.db.repositories.jobs import IngestionJobRepository, JobAlreadyRunningError
from app.ingestion.pipeline import DocumentResult, IngestionReport, Outcome
from app.services.jobs import IngestionJobService, PipelineFactory
from app.settings import get_settings

Factory = async_sessionmaker[AsyncSession]


class FakePipeline:
    """Stands in for IngestionPipeline: returns a report, or raises."""

    def __init__(self, report: IngestionReport | None = None, error: Exception | None = None):
        self.report = report if report is not None else IngestionReport()
        self.error = error
        self.runs = 0

    async def run(self) -> IngestionReport:
        self.runs += 1
        if self.error is not None:
            raise self.error
        return self.report


def service(session_factory: Factory, pipeline: FakePipeline) -> IngestionJobService:
    return IngestionJobService(
        session_factory,
        get_settings(),
        pipeline_factory=cast(PipelineFactory, lambda: pipeline),
    )


def report_of(*results: DocumentResult, opened: int = 0, unopened: int = 0) -> IngestionReport:
    return IngestionReport(results=list(results), opened=opened, unopened=unopened)


async def state_of(
    session_factory: Factory, job_id: uuid.UUID
) -> tuple[str, str, dict[str, object]]:
    async with session_factory() as session:
        job = await IngestionJobRepository(session).get(job_id)
        assert job is not None
        return job.state, job.detail, dict(job.report)


# --- claiming the slot ----------------------------------------------------------------------------


async def test_starting_a_run_claims_the_slot_and_records_it_as_running(
    session_factory: Factory,
) -> None:
    jobs = service(session_factory, FakePipeline())

    job = await jobs.start()

    assert job.state == JobState.RUNNING.value
    assert jobs.task is not None
    await jobs.task


async def test_a_second_run_is_refused_while_one_is_in_flight(session_factory: Factory) -> None:
    """Enforced by the partial unique index, so two workers cannot both think they won."""
    async with session_factory() as session, session.begin():
        await IngestionJobRepository(session).claim()
    jobs = service(session_factory, FakePipeline())

    with pytest.raises(JobAlreadyRunningError):
        await jobs.start()


async def test_the_slot_is_free_again_once_a_run_finishes(session_factory: Factory) -> None:
    first = service(session_factory, FakePipeline())
    await first.start()
    assert first.task is not None
    await first.task

    second = service(session_factory, FakePipeline())
    job = await second.start()

    assert job.state == JobState.RUNNING.value
    assert second.task is not None
    await second.task


async def test_releasing_a_dead_run_frees_the_slot(session_factory: Factory) -> None:
    """A worker killed mid-run would otherwise block ingestion until someone edited the table."""
    async with session_factory() as session, session.begin():
        stuck = await IngestionJobRepository(session).claim()
        stuck_id = stuck.id

    async with session_factory() as session, session.begin():
        assert await IngestionJobRepository(session).abandon(stuck_id) is True

    jobs = service(session_factory, FakePipeline())
    await jobs.start()
    assert jobs.task is not None
    await jobs.task
    assert (await state_of(session_factory, stuck_id))[0] == JobState.ABANDONED.value


async def test_a_finished_run_cannot_be_released(session_factory: Factory) -> None:
    """Abandoning is for a job believed dead; a finished state is final."""
    jobs = service(session_factory, FakePipeline())
    job = await jobs.start()
    assert jobs.task is not None
    await jobs.task

    async with session_factory() as session, session.begin():
        assert await IngestionJobRepository(session).abandon(job.id) is False


# --- recording the outcome ------------------------------------------------------------------------


async def test_a_clean_run_is_recorded_as_succeeded_with_its_counts(
    session_factory: Factory,
) -> None:
    pipeline = FakePipeline(
        report_of(
            DocumentResult("notes.md", Outcome.ADDED, "12 chunks"),
            DocumentResult("old.pdf", Outcome.SKIPPED, "unchanged, not opened"),
            opened=1,
            unopened=1,
        )
    )
    jobs = service(session_factory, pipeline)

    job = await jobs.start()
    assert jobs.task is not None
    await jobs.task

    state, detail, report = await state_of(session_factory, job.id)
    assert state == JobState.SUCCEEDED.value
    assert "1 added" in detail
    assert report["failures"] == []
    assert "opened 1 of 2 sources" in str(report["opening"])


async def test_a_run_with_a_failed_source_is_recorded_as_failed_and_names_it(
    session_factory: Factory,
) -> None:
    pipeline = FakePipeline(
        report_of(
            DocumentResult("good.md", Outcome.ADDED, "3 chunks"),
            DocumentResult("broken.pdf", Outcome.FAILED, "not a PDF"),
            opened=2,
        )
    )
    jobs = service(session_factory, pipeline)

    job = await jobs.start()
    assert jobs.task is not None
    await jobs.task

    state, detail, report = await state_of(session_factory, job.id)
    assert state == JobState.FAILED.value
    assert detail == "1 source(s) failed"
    assert report["failures"] == ["broken.pdf: not a PDF"]


async def test_a_run_that_raises_is_recorded_as_failed_rather_than_left_running(
    session_factory: Factory,
) -> None:
    """A job row stuck at `running` would block every later run, so this must never happen."""
    broken = FakePipeline(error=RuntimeError("OPENAI_API_KEY is not set"))
    jobs = service(session_factory, broken)

    job = await jobs.start()
    assert jobs.task is not None
    await jobs.task

    state, detail, _ = await state_of(session_factory, job.id)
    assert state == JobState.FAILED.value
    assert "RuntimeError" in detail


async def test_the_pipeline_is_built_once_per_run(session_factory: Factory) -> None:
    pipeline = FakePipeline()
    jobs = service(session_factory, pipeline)

    await jobs.start()
    assert jobs.task is not None
    await jobs.task

    assert pipeline.runs == 1
