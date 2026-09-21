import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EMBEDDING_DIMENSIONS, Chunk, ChunkVector, Source
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository
from vector_store.base import VectorFilter, VectorRecord, VectorStore
from vector_store.pgvector_store import PgVectorStore


def unit_vector(axis: int) -> list[float]:
    """A unit vector along one axis; different axes are orthogonal (cosine similarity 0)."""
    vec = [0.0] * EMBEDDING_DIMENSIONS
    vec[axis] = 1.0
    return vec


async def seed_source(session: AsyncSession, uri: str, source_type: str = "pdf") -> Source:
    return await SourceRepository(session).create(
        source_type=source_type, source_uri=uri, content_hash="h"
    )


async def seed_chunk(session: AsyncSession, source: Source, index: int) -> Chunk:
    [chunk] = await ChunkRepository(session).add_many(
        [
            NewChunk(
                id=uuid.uuid4(),
                source_id=source.id,
                chunk_index=index,
                text=f"chunk {index}",
                token_count=2,
                embedding_model="text-embedding-3-small",
            )
        ]
    )
    return chunk


def record(chunk: Chunk, source: Source, axis: int) -> VectorRecord:
    return VectorRecord(
        chunk_id=chunk.id,
        embedding=unit_vector(axis),
        source_id=source.id,
        source_type=source.source_type,
        chunk_index=chunk.chunk_index,
    )


async def vector_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(ChunkVector)) or 0


def test_pgvector_store_satisfies_protocol(session: AsyncSession) -> None:
    store: VectorStore = PgVectorStore(session)
    assert store is not None


async def test_search_returns_nearest_first_with_scores(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    c0, c1, c2 = [await seed_chunk(session, source, i) for i in range(3)]
    store = PgVectorStore(session)
    await store.upsert([record(c0, source, 0), record(c1, source, 1), record(c2, source, 2)])

    query = [0.0] * EMBEDDING_DIMENSIONS
    query[1], query[2] = 0.9, 0.1  # closest to c1, then c2, orthogonal to c0
    matches = await store.search(query, top_k=3)

    assert [m.chunk_id for m in matches] == [c1.id, c2.id, c0.id]
    assert matches[0].score > matches[1].score > matches[2].score
    assert matches[2].score == pytest.approx(0.0, abs=1e-6)


async def test_identical_vector_scores_one(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunk = await seed_chunk(session, source, 0)
    store = PgVectorStore(session)
    await store.upsert([record(chunk, source, 5)])

    [match] = await store.search(unit_vector(5), top_k=1)
    assert match.chunk_id == chunk.id
    assert match.score == pytest.approx(1.0, abs=1e-6)


async def test_top_k_limits_results(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunks = [await seed_chunk(session, source, i) for i in range(4)]
    store = PgVectorStore(session)
    await store.upsert([record(c, source, i) for i, c in enumerate(chunks)])

    assert len(await store.search(unit_vector(0), top_k=2)) == 2


async def test_source_type_filter(session: AsyncSession) -> None:
    pdf = await seed_source(session, "a", "pdf")
    web = await seed_source(session, "b", "web")
    pdf_chunk = await seed_chunk(session, pdf, 0)
    web_chunk = await seed_chunk(session, web, 0)
    store = PgVectorStore(session)
    await store.upsert([record(pdf_chunk, pdf, 0), record(web_chunk, web, 0)])

    only_web = await store.search(unit_vector(0), 10, VectorFilter(source_type="web"))
    assert [m.chunk_id for m in only_web] == [web_chunk.id]
    assert len(await store.search(unit_vector(0), 10, VectorFilter())) == 2
    assert await store.search(unit_vector(0), 10, VectorFilter(source_type="csv")) == []


async def test_upsert_replaces_existing_vector(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunk = await seed_chunk(session, source, 0)
    store = PgVectorStore(session)
    await store.upsert([record(chunk, source, 0)])
    updated = VectorRecord(
        chunk_id=chunk.id,
        embedding=unit_vector(7),
        source_id=source.id,
        source_type="pdf",
        chunk_index=0,
        metadata={"revision": 2},
    )
    await store.upsert([updated])

    assert await vector_count(session) == 1
    stored = await session.get(ChunkVector, chunk.id, populate_existing=True)
    assert stored is not None and stored.metadata_ == {"revision": 2}
    [match] = await store.search(unit_vector(7), top_k=1)
    assert match.score == pytest.approx(1.0, abs=1e-6)


async def test_upsert_stores_metadata(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunk = await seed_chunk(session, source, 0)
    rec = VectorRecord(
        chunk_id=chunk.id,
        embedding=unit_vector(0),
        source_id=source.id,
        source_type="pdf",
        chunk_index=0,
        metadata={"tags": ["policy"], "date": "2026-01-01"},
    )
    await PgVectorStore(session).upsert([rec])

    stored = await session.get(ChunkVector, chunk.id)
    assert stored is not None
    assert stored.metadata_ == {"tags": ["policy"], "date": "2026-01-01"}


async def test_empty_upsert_and_delete_are_noops(session: AsyncSession) -> None:
    store = PgVectorStore(session)
    await store.upsert([])
    assert await store.delete([]) == 0
    assert await vector_count(session) == 0


async def test_wrong_dimensions_rejected(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunk = await seed_chunk(session, source, 0)
    store = PgVectorStore(session)
    bad = VectorRecord(
        chunk_id=chunk.id,
        embedding=[0.1, 0.2, 0.3],
        source_id=source.id,
        source_type="pdf",
        chunk_index=0,
    )
    with pytest.raises(ValueError, match="dimensions"):
        await store.upsert([bad])
    with pytest.raises(ValueError, match="dimensions"):
        await store.search([0.1, 0.2], top_k=1)


async def test_delete_by_chunk_ids(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    c0, c1 = await seed_chunk(session, source, 0), await seed_chunk(session, source, 1)
    store = PgVectorStore(session)
    await store.upsert([record(c0, source, 0), record(c1, source, 1)])

    assert await store.delete([c0.id, uuid.uuid4()]) == 1
    assert [m.chunk_id for m in await store.search(unit_vector(0), 10)] == [c1.id]


async def test_delete_by_source_leaves_other_sources(session: AsyncSession) -> None:
    s1, s2 = await seed_source(session, "one"), await seed_source(session, "two")
    c1, c2 = await seed_chunk(session, s1, 0), await seed_chunk(session, s2, 0)
    store = PgVectorStore(session)
    await store.upsert([record(c1, s1, 0), record(c2, s2, 1)])

    assert await store.delete_by_source(s1.id) == 1
    assert [m.chunk_id for m in await store.search(unit_vector(0), 10)] == [c2.id]


async def test_vectors_require_an_existing_chunk(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    orphan = VectorRecord(
        chunk_id=uuid.uuid4(),
        embedding=unit_vector(0),
        source_id=source.id,
        source_type="pdf",
        chunk_index=0,
    )
    with pytest.raises(IntegrityError):
        await PgVectorStore(session).upsert([orphan])


async def test_deleting_chunks_cascades_to_vectors(session: AsyncSession) -> None:
    source = await seed_source(session, "a")
    chunk = await seed_chunk(session, source, 0)
    await PgVectorStore(session).upsert([record(chunk, source, 0)])

    await ChunkRepository(session).delete_by_source(source.id)

    assert await vector_count(session) == 0


async def test_upsert_larger_than_one_batch(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vector_store.pgvector_store as store_module

    monkeypatch.setattr(store_module, "UPSERT_BATCH_SIZE", 2)
    source = await SourceRepository(session).create(
        source_type="file", source_uri="big.md", content_hash="h"
    )
    ids = [uuid.uuid4() for _ in range(5)]
    await ChunkRepository(session).add_many(
        [NewChunk(cid, source.id, i, "t", 1, "m") for i, cid in enumerate(ids)]
    )
    store = PgVectorStore(session)
    vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

    await store.upsert(
        [VectorRecord(cid, vector, source.id, "file", i) for i, cid in enumerate(ids)]
    )

    matches = await store.search(vector, top_k=10)
    assert {m.chunk_id for m in matches} == set(ids)
