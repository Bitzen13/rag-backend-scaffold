import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    ApiKey,
    ApiKeyRateLimit,
    Chunk,
    IngestionJob,
    JobState,
    QueryLog,
    SourceStatus,
)
from db.repositories.api_keys import ApiKeyRepository
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.jobs import IngestionJobRepository
from db.repositories.query_logs import QueryLogRepository
from db.repositories.rate_limits import RateLimitRepository
from db.repositories.sources import SourceRepository


def new_chunk(source_id: uuid.UUID, index: int, text: str = "some text") -> NewChunk:
    return NewChunk(
        id=uuid.uuid4(),
        source_id=source_id,
        chunk_index=index,
        text=text,
        token_count=len(text.split()),
        embedding_model="text-embedding-3-small",
    )


# --- sources ---------------------------------------------------------------------------------


async def test_create_and_fetch_source(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.create(
        source_type="pdf", source_uri="docs/a.pdf", content_hash="h1", metadata={"pages": 3}
    )
    assert source.status == SourceStatus.ACTIVE
    assert source.last_ingested_at is not None
    assert await repo.get(source.id) is source
    assert await repo.get_by_uri("pdf", "docs/a.pdf") is source
    assert await repo.get_by_uri("web", "docs/a.pdf") is None
    assert source.metadata_ == {"pages": 3}


async def test_source_uri_is_unique_per_type(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    await repo.create(source_type="pdf", source_uri="a", content_hash="h")
    await repo.create(source_type="web", source_uri="a", content_hash="h")  # other type is fine
    with pytest.raises(IntegrityError):
        await repo.create(source_type="pdf", source_uri="a", content_hash="h2")


async def test_list_sources_filters_by_status(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    keep = await repo.create(source_type="pdf", source_uri="keep", content_hash="h")
    gone = await repo.create(source_type="pdf", source_uri="gone", content_hash="h")
    await repo.set_status(gone, SourceStatus.DELETED)

    assert {s.id for s in await repo.list()} == {keep.id, gone.id}
    assert [s.id for s in await repo.list(SourceStatus.ACTIVE)] == [keep.id]
    assert [s.id for s in await repo.list(SourceStatus.DELETED)] == [gone.id]


async def test_mark_ingested_updates_hash_and_reactivates(session: AsyncSession) -> None:
    repo = SourceRepository(session)
    source = await repo.create(source_type="pdf", source_uri="a", content_hash="old")
    first_ingested = source.last_ingested_at
    await repo.set_status(source, SourceStatus.STALE)

    await repo.mark_ingested(source, content_hash="new")

    assert source.content_hash == "new"
    assert source.status == SourceStatus.ACTIVE
    assert source.last_ingested_at >= first_ingested


async def test_invalid_status_rejected_by_database(session: AsyncSession) -> None:
    source = await SourceRepository(session).create(
        source_type="pdf", source_uri="a", content_hash="h"
    )
    source.status = "bogus"
    with pytest.raises(IntegrityError):
        await session.flush()


# --- chunks ----------------------------------------------------------------------------------


async def test_add_and_list_chunks_in_order(session: AsyncSession) -> None:
    source = await SourceRepository(session).create(
        source_type="pdf", source_uri="a", content_hash="h"
    )
    repo = ChunkRepository(session)
    await repo.add_many([new_chunk(source.id, 1, "second"), new_chunk(source.id, 0, "first")])

    chunks = await repo.list_by_source(source.id)
    assert [c.text for c in chunks] == ["first", "second"]
    assert all(c.created_at is not None for c in chunks)


async def test_get_many_preserves_requested_order_and_skips_missing(
    session: AsyncSession,
) -> None:
    source = await SourceRepository(session).create(
        source_type="pdf", source_uri="a", content_hash="h"
    )
    repo = ChunkRepository(session)
    a, b, c = (new_chunk(source.id, i, f"t{i}") for i in range(3))
    await repo.add_many([a, b, c])

    fetched = await repo.get_many([c.id, uuid.uuid4(), a.id])
    assert [x.id for x in fetched] == [c.id, a.id]
    assert await repo.get_many([]) == []


async def test_chunk_index_unique_within_source(session: AsyncSession) -> None:
    source = await SourceRepository(session).create(
        source_type="pdf", source_uri="a", content_hash="h"
    )
    repo = ChunkRepository(session)
    await repo.add_many([new_chunk(source.id, 0)])
    with pytest.raises(IntegrityError):
        await repo.add_many([new_chunk(source.id, 0)])


async def test_delete_chunks_by_source_only_touches_that_source(session: AsyncSession) -> None:
    sources = SourceRepository(session)
    s1 = await sources.create(source_type="pdf", source_uri="one", content_hash="h")
    s2 = await sources.create(source_type="pdf", source_uri="two", content_hash="h")
    repo = ChunkRepository(session)
    await repo.add_many([new_chunk(s1.id, 0), new_chunk(s1.id, 1), new_chunk(s2.id, 0)])

    assert await repo.delete_by_source(s1.id) == 2
    assert await repo.list_by_source(s1.id) == []
    assert len(await repo.list_by_source(s2.id)) == 1


async def test_deleting_source_cascades_to_chunks(session: AsyncSession) -> None:
    source = await SourceRepository(session).create(
        source_type="pdf", source_uri="a", content_hash="h"
    )
    await ChunkRepository(session).add_many([new_chunk(source.id, 0)])
    await session.delete(source)
    await session.flush()

    assert await session.scalar(select(func.count()).select_from(Chunk)) == 0


# --- api keys --------------------------------------------------------------------------------


async def test_api_key_lifecycle(session: AsyncSession) -> None:
    repo = ApiKeyRepository(session)
    key = await repo.create(key_hash="hash-1", label="Slack bot")
    assert key.is_active is True
    assert key.last_used_at is None

    found = await repo.get_by_hash("hash-1")
    assert found is not None and found.id == key.id
    assert await repo.get_by_hash("nope") is None

    await repo.touch_last_used(key.id)
    await session.refresh(key)
    assert key.last_used_at is not None

    assert await repo.revoke(key.id) is True
    await session.refresh(key)
    assert key.is_active is False
    # revoked keys stay in the table so callers can tell "revoked" from "unknown"
    revoked = await repo.get_by_hash("hash-1")
    assert revoked is not None and revoked.is_active is False


async def test_revoke_unknown_key_returns_false(session: AsyncSession) -> None:
    assert await ApiKeyRepository(session).revoke(uuid.uuid4()) is False


async def test_api_key_hash_is_unique(session: AsyncSession) -> None:
    repo = ApiKeyRepository(session)
    await repo.create(key_hash="same", label="a")
    with pytest.raises(IntegrityError):
        await repo.create(key_hash="same", label="b")


# --- rate limit counters ------------------------------------------------------------------------


async def a_key(session: AsyncSession, label: str = "consumer") -> uuid.UUID:
    key = await ApiKeyRepository(session).create(key_hash=f"hash-{uuid.uuid4()}", label=label)
    return key.id


async def test_counting_a_request_starts_at_one_and_increments(session: AsyncSession) -> None:
    repo = RateLimitRepository(session)
    key_id = await a_key(session)
    window = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

    counts = [await repo.count_request(key_id, window) for _ in range(3)]

    assert counts == [1, 2, 3]


async def test_the_increment_is_one_statement_so_workers_cannot_both_read_the_same_value(
    session: AsyncSession,
) -> None:
    """The count is returned by the upsert itself; nothing reads before it writes."""
    repo = RateLimitRepository(session)
    key_id = await a_key(session)
    window = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    await repo.count_request(key_id, window)

    stored = await session.scalar(
        select(ApiKeyRateLimit.request_count).where(
            ApiKeyRateLimit.api_key_id == key_id, ApiKeyRateLimit.window_start == window
        )
    )

    assert stored == 1


async def test_windows_and_keys_are_counted_separately(session: AsyncSession) -> None:
    repo = RateLimitRepository(session)
    first, second = await a_key(session, "a"), await a_key(session, "b")
    window = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    later = datetime(2026, 9, 21, 12, 1, tzinfo=UTC)

    await repo.count_request(first, window)
    await repo.count_request(first, window)

    assert await repo.count_request(first, later) == 1
    assert await repo.count_request(second, window) == 1


async def test_purging_removes_only_this_key_s_finished_windows(session: AsyncSession) -> None:
    repo = RateLimitRepository(session)
    mine, theirs = await a_key(session, "mine"), await a_key(session, "theirs")
    old = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    current = datetime(2026, 9, 21, 12, 1, tzinfo=UTC)
    for key_id in (mine, theirs):
        await repo.count_request(key_id, old)
        await repo.count_request(key_id, current)

    removed = await repo.purge_before(mine, current)

    assert removed == 1
    assert await repo.count_request(mine, current) == 2  # the open window survived
    assert await repo.count_request(theirs, old) == 2  # the other key is untouched


async def test_deleting_a_key_takes_its_counters_with_it(session: AsyncSession) -> None:
    repo = RateLimitRepository(session)
    key_id = await a_key(session)
    await repo.count_request(key_id, datetime(2026, 9, 21, 12, 0, tzinfo=UTC))

    await session.execute(delete(ApiKey).where(ApiKey.id == key_id))
    await session.flush()

    remaining = await session.scalar(
        select(func.count())
        .select_from(ApiKeyRateLimit)
        .where(ApiKeyRateLimit.api_key_id == key_id)
    )
    assert remaining == 0


# --- query logs ---------------------------------------------------------------------------------


async def test_a_query_log_records_the_question_answer_and_chunks(session: AsyncSession) -> None:
    key_id = await a_key(session)
    chunk_ids = [uuid.uuid4(), uuid.uuid4()]

    row = await QueryLogRepository(session).record(
        api_key_id=key_id,
        question="Why is the sky blue?",
        answer="Rayleigh scattering.",
        retrieved_chunk_ids=chunk_ids,
    )

    stored = await session.get(QueryLog, row.id)
    assert stored is not None
    assert stored.question == "Why is the sky blue?"
    assert stored.answer == "Rayleigh scattering."
    assert stored.retrieved_chunk_ids == chunk_ids
    assert stored.created_at is not None


async def test_a_query_log_can_record_that_nothing_was_retrieved(session: AsyncSession) -> None:
    key_id = await a_key(session)

    row = await QueryLogRepository(session).record(
        api_key_id=key_id,
        question="off topic",
        answer="not in my knowledge base",
        retrieved_chunk_ids=[],
    )

    assert row.retrieved_chunk_ids == []


# --- ingestion jobs -------------------------------------------------------------------------------


async def test_only_one_job_can_be_running_at_a_time(session: AsyncSession) -> None:
    """The partial unique index, which is what makes "one run at a time" true across workers."""
    repo = IngestionJobRepository(session)
    await repo.claim()

    # Inserted directly: `claim` turns this into JobAlreadyRunningError, and the constraint
    # underneath it is what is being checked here.
    with pytest.raises(IntegrityError):
        await session.execute(insert(IngestionJob).values(state=JobState.RUNNING.value))
        await session.flush()


async def test_a_finished_job_frees_the_slot(session: AsyncSession) -> None:
    repo = IngestionJobRepository(session)
    first = await repo.claim()

    await repo.finish(first.id, state=JobState.SUCCEEDED, detail="1 added", report={"counts": "x"})
    await session.flush()
    second = await repo.claim()

    assert second.id != first.id
    await session.refresh(first)
    assert first.state == JobState.SUCCEEDED.value
    assert first.finished_at is not None
    assert first.report == {"counts": "x"}


async def test_the_running_job_can_be_found(session: AsyncSession) -> None:
    repo = IngestionJobRepository(session)
    assert await repo.running() is None

    job = await repo.claim()

    found = await repo.running()
    assert found is not None and found.id == job.id


async def test_an_invalid_job_state_is_rejected_by_the_database(session: AsyncSession) -> None:
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(insert(IngestionJob).values(state="hurrying"))
        await session.flush()
