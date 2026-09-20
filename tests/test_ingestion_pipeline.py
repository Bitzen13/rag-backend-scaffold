import hashlib
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chunking.fixed_size import FixedSizeChunker
from db.models import EMBEDDING_DIMENSIONS, Chunk, ChunkVector, Source
from embedding.base import EmbeddingError
from extraction.base import ExtractedDocument
from ingestion.pipeline import IngestionPipeline, Outcome
from vector_store.pgvector_store import PgVectorStore


class FakeExtractor:
    def __init__(self, documents: Iterable[ExtractedDocument]) -> None:
        self.documents = list(documents)

    def extract(self) -> Iterable[ExtractedDocument]:
        return list(self.documents)


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
        chunker=FixedSizeChunker(chunk_size=5, chunk_overlap=1),
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
