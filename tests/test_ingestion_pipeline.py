import hashlib
import logging
from collections.abc import Iterable, Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chunking.base import Chunker, ChunkingStrategyError, ChunkPlan
from chunking.fixed_size import FixedSizeChunker
from chunking.tokenizer import WordTokenizer
from db.models import EMBEDDING_DIMENSIONS, Chunk, ChunkVector, Source
from embedding.base import EmbeddingError
from extraction.base import ExtractedDocument
from ingestion.pipeline import IngestionPipeline, Outcome, ProgressTracker, format_duration
from vector_store.pgvector_store import PgVectorStore


class FakeExtractor:
    def __init__(self, documents: Iterable[ExtractedDocument]) -> None:
        self.documents = list(documents)

    def extract(self) -> Iterable[ExtractedDocument]:
        return list(self.documents)


PLAN = ChunkPlan(
    strategy="fixed_size",
    chunk_size=5,
    chunk_overlap=1,
    prose_splitting="paragraph",
    max_chunk_tokens=1000,
    tokenizer="words",
)


class FakeChunkers:
    """Resolver that only knows the documents it is given (no default strategy)."""

    def __init__(self, known: set[str]) -> None:
        self._known = known
        self._chunker = FixedSizeChunker(PLAN, WordTokenizer())

    def plan_for(self, source_uri: str) -> ChunkPlan:
        if source_uri not in self._known:
            raise ChunkingStrategyError(f"no chunking strategy set for {source_uri!r}")
        return PLAN

    def for_document(self, source_uri: str) -> Chunker:
        self.plan_for(source_uri)
        return self._chunker


class FakeEmbedder:
    model_name = "fake-embedding"

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls = 0
        self._fail_on = fail_on

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self._fail_on is not None and any(self._fail_on in t for t in texts):
            raise EmbeddingError("boom")
        return [_vector(t) for t in texts]


def _vector(text: str) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    return [seed[i % len(seed)] / 255 + 0.01 for i in range(EMBEDDING_DIMENSIONS)]


def doc(uri: str, text: str) -> ExtractedDocument:
    return ExtractedDocument("file", uri, text, {"filename": uri})


def make_pipeline(
    session: AsyncSession, documents: list[ExtractedDocument], embedder: FakeEmbedder
) -> IngestionPipeline:
    return IngestionPipeline(
        extractor=FakeExtractor(documents),
        chunkers=FakeChunkers({d.source_uri for d in documents if d.source_uri != "nostrategy.md"}),
        embedder=embedder,
        session_factory=lambda: session,
        vector_store_factory=PgVectorStore,
    )


async def count(
    session: AsyncSession, model: type[Source] | type[Chunk] | type[ChunkVector]
) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


TEXT = " ".join(f"word{i}" for i in range(12))  # 12 words -> 3 chunks of 5 with overlap 1


async def test_ingest_populates_sources_chunks_and_vectors(session: AsyncSession) -> None:
    report = await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()

    assert [(r.source_uri, r.outcome) for r in report.results] == [("a.md", Outcome.ADDED)]
    source = (await session.execute(select(Source))).scalar_one()
    assert source.content_hash == hashlib.sha256(TEXT.encode()).hexdigest()
    assert (source.source_type, source.source_uri, source.status) == ("file", "a.md", "active")
    assert source.metadata_ == {"filename": "a.md"}

    chunks = (await session.execute(select(Chunk).order_by(Chunk.chunk_index))).scalars().all()
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert all(c.embedding_model == "fake-embedding" for c in chunks)
    vectors = (await session.execute(select(ChunkVector))).scalars().all()
    assert {v.chunk_id for v in vectors} == {c.id for c in chunks}
    assert vectors[0].metadata_["source_uri"] == "a.md"


async def test_unchanged_source_is_skipped_without_embedding(session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await make_pipeline(session, [doc("a.md", TEXT)], embedder).run()
    calls = embedder.calls

    report = await make_pipeline(session, [doc("a.md", TEXT)], embedder).run()

    assert [r.outcome for r in report.results] == [Outcome.SKIPPED]
    assert embedder.calls == calls
    assert await count(session, Chunk) == 3


async def test_changed_source_replaces_chunks_and_vectors(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    old_ids = set((await session.execute(select(Chunk.id))).scalars())
    await session.commit()  # the pipeline opens its own transaction on this shared session

    report = await make_pipeline(
        session, [doc("a.md", "just four words here")], FakeEmbedder()
    ).run()

    assert [r.outcome for r in report.results] == [Outcome.UPDATED]
    new_ids = set((await session.execute(select(Chunk.id))).scalars())
    assert len(new_ids) == 1
    assert not new_ids & old_ids
    assert await count(session, ChunkVector) == 1
    source = (await session.execute(select(Source))).scalar_one()
    assert source.content_hash == hashlib.sha256(b"just four words here").hexdigest()


async def test_blank_document_is_skipped(session: AsyncSession) -> None:
    report = await make_pipeline(session, [doc("blank.md", "  \n ")], FakeEmbedder()).run()
    assert [r.outcome for r in report.results] == [Outcome.SKIPPED]
    assert await count(session, Source) == 0


async def test_failed_document_writes_nothing_and_run_continues(session: AsyncSession) -> None:
    documents = [doc("bad.md", "explode " + TEXT), doc("good.md", TEXT)]
    report = await make_pipeline(session, documents, FakeEmbedder(fail_on="explode")).run()

    assert [(r.source_uri, r.outcome) for r in report.results] == [
        ("bad.md", Outcome.FAILED),
        ("good.md", Outcome.ADDED),
    ]
    sources = (await session.execute(select(Source.source_uri))).scalars().all()
    assert sources == ["good.md"]


async def test_document_without_a_chunking_strategy_fails_and_others_still_ingest(
    session: AsyncSession,
) -> None:
    documents = [doc("nostrategy.md", TEXT), doc("good.md", TEXT)]

    report = await make_pipeline(session, documents, FakeEmbedder()).run()

    assert [(r.source_uri, r.outcome) for r in report.results] == [
        ("nostrategy.md", Outcome.FAILED),
        ("good.md", Outcome.ADDED),
    ]
    assert "no chunking strategy" in report.results[0].detail
    assert (await session.execute(select(Source.source_uri))).scalars().all() == ["good.md"]


async def test_chunk_metadata_is_stored_on_vectors(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    vector = (await session.execute(select(ChunkVector).limit(1))).scalars().one()
    assert vector.metadata_["source_uri"] == "a.md"
    assert "char_start" in vector.metadata_


# --- progress logging ------------------------------------------------------------------------


def test_format_duration() -> None:
    assert [format_duration(s) for s in (5, 65, 3725)] == ["5s", "1m 05s", "1h 02m"]


def test_progress_logs_count_percent_and_time_remaining(caplog: pytest.LogCaptureFixture) -> None:
    now = [0.0]
    progress = ProgressTracker(total=200, clock=lambda: now[0])

    with caplog.at_level(logging.INFO, logger="ingestion.pipeline"):
        now[0] = 30.0
        progress.advance(50)  # 25% in 30s -> 90s left
        now[0] = 120.0
        progress.advance(150)  # done: no estimate

    assert [r.getMessage() for r in caplog.records] == [
        "processed 50/200 chunks (25%), elapsed 30s, ~1m 30s remaining",
        "processed 200/200 chunks (100%), elapsed 2m 00s",
    ]


async def test_run_logs_progress_per_embedding_batch(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="ingestion.pipeline"):
        await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()

    messages = [r.getMessage() for r in caplog.records]
    assert "chunked 1 documents into 3 chunks" in messages
    assert any(m.startswith("processed 3/3 chunks (100%)") for m in messages)
