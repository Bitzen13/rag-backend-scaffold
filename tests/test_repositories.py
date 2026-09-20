import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Chunk, SourceStatus
from db.repositories.api_keys import ApiKeyRepository
from db.repositories.chunks import ChunkRepository, NewChunk
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
