import logging
import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import EMBEDDING_DIMENSIONS, SourceStatus
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository
from embedding.base import EmbeddingError
from retrieval.retriever import Retriever
from vector_store.base import VectorFilter, VectorRecord
from vector_store.pgvector_store import PgVectorStore

TOPICS = ("cats", "dogs", "birds")


def topic_vector(topic: str) -> list[float]:
    """Unit vector per topic: same topic -> similarity 1.0, different topics -> 0.0."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[TOPICS.index(topic)] = 1.0
    return vector


class TopicEmbedder:
    model_name = "fake-embedding"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [topic_vector(next(t for t in TOPICS if t in text)) for text in texts]


class WrongCountEmbedder:
    model_name = "fake-embedding"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return []


async def add_source(
    session: AsyncSession,
    uri: str,
    topics: Sequence[str],
    *,
    source_type: str = "file",
    status: SourceStatus = SourceStatus.ACTIVE,
) -> dict[str, uuid.UUID]:
    """Create a source with one chunk per topic; returns topic -> chunk id."""
    source = await SourceRepository(session).create(
        source_type=source_type, source_uri=uri, content_hash="h"
    )
    if status is not SourceStatus.ACTIVE:
        await SourceRepository(session).set_status(source, status)
    chunk_ids = {topic: uuid.uuid4() for topic in topics}
    await ChunkRepository(session).add_many(
        [
            NewChunk(chunk_ids[t], source.id, i, f"text about {t}", 3, "fake-embedding")
            for i, t in enumerate(topics)
        ]
    )
    await PgVectorStore(session).upsert(
        [
            VectorRecord(chunk_ids[t], topic_vector(t), source.id, source_type, i)
            for i, t in enumerate(topics)
        ]
    )
    return chunk_ids


def make_retriever(
    session: AsyncSession,
    *,
    top_k: int = 5,
    threshold: float = 0.75,
    embedder: TopicEmbedder | WrongCountEmbedder | None = None,
    fuzzy_top_k: int = 0,
) -> Retriever:
    return Retriever(
        embedder=embedder or TopicEmbedder(),
        vector_store=PgVectorStore(session),
        chunks=ChunkRepository(session),
        top_k=top_k,
        similarity_threshold=threshold,
        fuzzy_top_k=fuzzy_top_k,
    )


async def test_returns_matching_chunk_with_text_and_source(session: AsyncSession) -> None:
    ids = await add_source(session, "pets.md", ["cats", "dogs"])

    results = await make_retriever(session).retrieve("tell me about cats")

    assert len(results) == 1
    assert results[0].chunk_id == ids["cats"]
    assert results[0].source_uri == "pets.md"
    assert results[0].text == "text about cats"
    assert results[0].score == pytest.approx(1.0)


async def test_nothing_above_threshold_returns_empty(session: AsyncSession) -> None:
    await add_source(session, "pets.md", ["cats", "dogs"])
    assert await make_retriever(session).retrieve("what about birds") == []


async def test_threshold_is_inclusive_and_configurable(session: AsyncSession) -> None:
    await add_source(session, "pets.md", ["cats", "dogs"])
    results = await make_retriever(session, threshold=0.0).retrieve("cats")
    assert len(results) == 2
    assert results[0].text == "text about cats"  # best first


async def test_top_k_limits_results(session: AsyncSession) -> None:
    await add_source(session, "pets.md", ["cats", "dogs", "birds"])
    results = await make_retriever(session, top_k=1, threshold=0.0).retrieve("cats")
    assert [r.text for r in results] == ["text about cats"]


async def test_source_type_filter(session: AsyncSession) -> None:
    await add_source(session, "a.md", ["cats"], source_type="file")
    await add_source(session, "https://x", ["cats"], source_type="web")

    results = await make_retriever(session).retrieve("cats", VectorFilter(source_type="web"))

    assert [r.source_uri for r in results] == ["https://x"]


async def test_stale_and_deleted_sources_are_excluded(session: AsyncSession) -> None:
    await add_source(session, "stale.md", ["cats"], status=SourceStatus.STALE)
    await add_source(session, "gone.md", ["cats"], status=SourceStatus.DELETED)
    assert await make_retriever(session).retrieve("cats") == []


async def test_bad_embedding_count_raises(session: AsyncSession) -> None:
    with pytest.raises(EmbeddingError):
        await make_retriever(session, embedder=WrongCountEmbedder()).retrieve("cats")


async def test_debug_logs_every_candidate_with_score_and_verdict(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await add_source(session, "pets.md", ["cats", "dogs"])

    with caplog.at_level(logging.DEBUG, logger="retrieval.retriever"):
        results = await make_retriever(session).retrieve("tell me about cats")

    assert len(results) == 1  # only the chunk above the threshold is returned
    messages = [r.getMessage() for r in caplog.records]
    assert "2 vector candidates" in messages[0]
    assert "vector #1 score=1.000 [PASSED] pets.md" in messages[1]
    assert "text about cats" in messages[1]
    assert "vector #2 score=0.000 [below threshold] pets.md" in messages[2]
    assert "text about dogs" in messages[2]


async def test_no_candidate_logging_by_default(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await add_source(session, "pets.md", ["cats"])
    with caplog.at_level(logging.INFO, logger="retrieval.retriever"):
        await make_retriever(session).retrieve("cats")
    assert caplog.records == []


# --- hybrid: vector + fuzzy keyword ----------------------------------------------------------


async def add_named_chunk(session: AsyncSession, uri: str, text: str, topic: str) -> uuid.UUID:
    """A chunk with arbitrary text whose vector points at `topic`."""
    source = await SourceRepository(session).create(
        source_type="file", source_uri=uri, content_hash="h"
    )
    chunk_id = uuid.uuid4()
    await ChunkRepository(session).add_many([NewChunk(chunk_id, source.id, 0, text, 3, "m")])
    await PgVectorStore(session).upsert(
        [VectorRecord(chunk_id, topic_vector(topic), source.id, "file", 0)]
    )
    return chunk_id


async def test_fuzzy_finds_a_chunk_the_vector_search_misses(session: AsyncSession) -> None:
    # The question is about birds (no bird chunk exists), so no vector hit passes the threshold,
    # but the chunk containing the misspelled name is still found by spelling.
    target = await add_named_chunk(session, "z.md", "Zorblax Prime is a very unusual star", "dogs")

    results = await make_retriever(session, fuzzy_top_k=5).retrieve("birds and zorblaks prime")

    assert [r.chunk_id for r in results] == [target]
    assert results[0].score is None
    assert results[0].fuzzy_score is not None and results[0].fuzzy_score >= 0.6


async def test_chunk_found_by_both_methods_ranks_first(session: AsyncSession) -> None:
    both = await add_named_chunk(session, "a.md", "Zorblax Prime and cats", "cats")
    fuzzy_only = await add_named_chunk(session, "b.md", "Zorblax Prime alone", "dogs")

    results = await make_retriever(session, fuzzy_top_k=5).retrieve("cats zorblax prime")

    assert [r.chunk_id for r in results] == [both, fuzzy_only]
    assert results[0].score is not None and results[0].fuzzy_score is not None


async def test_fuzzy_search_is_off_by_default(session: AsyncSession) -> None:
    await add_named_chunk(session, "z.md", "Zorblax Prime is a very unusual star", "dogs")
    assert await make_retriever(session).retrieve("birds and zorblax prime") == []


async def test_debug_log_lists_fuzzy_hits(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await add_named_chunk(session, "z.md", "Zorblax Prime is a very unusual star", "dogs")

    with caplog.at_level(logging.DEBUG, logger="retrieval.retriever"):
        await make_retriever(session, fuzzy_top_k=5).retrieve("birds and zorblax prime")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "fuzzy keyword search: 1 hits" in text
    assert "best phrase 'zorblax prime'" in text
