import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chunking.base import Chunker, ChunkingStrategyError, ChunkPlan
from app.chunking.fixed_size import FixedSizeChunker
from app.chunking.tokenizer import WordTokenizer
from app.db.models import EMBEDDING_DIMENSIONS, Chunk, ChunkVector, Source
from app.embedding.base import EmbeddingError
from app.extraction.base import ExtractedDocument, ExtractionError, SourceCandidate
from app.extraction.limits import LimitExceededError
from app.ingestion.pipeline import (
    IngestionLimits,
    IngestionPipeline,
    MassDeletionError,
    Outcome,
    ProgressTracker,
    format_duration,
)
from app.vector_store.pgvector_store import PgVectorStore


@dataclass(frozen=True, kw_only=True)
class FakeCandidate(SourceCandidate):
    """A source whose text is held in memory, and which records whether it was ever opened."""

    document: ExtractedDocument | None  # None means the source cannot be read
    opened: list[str]

    def read(self) -> ExtractedDocument:
        self.opened.append(self.source_uri)
        if self.document is None:
            raise ExtractionError(f"{self.source_uri}: unreadable")
        return self.document


class FakeExtractor:
    source_type = "file"

    def __init__(self, documents: Sequence[ExtractedDocument | FakeCandidate]) -> None:
        self.opened: list[str] = []
        self._documents = list(documents)

    def candidates(self) -> list[FakeCandidate]:
        candidates = []
        for entry in self._documents:
            if isinstance(entry, FakeCandidate):
                candidates.append(replace(entry, opened=self.opened))
            else:
                candidates.append(
                    FakeCandidate(
                        source_type=entry.source_type,
                        source_uri=entry.source_uri,
                        source_hash=hashlib.sha256(entry.text.encode()).hexdigest(),
                        reader="fake",
                        reader_version=1,
                        document=entry,
                        opened=self.opened,
                    )
                )
        return candidates


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

    def __init__(self, known: set[str], plan: ChunkPlan = PLAN) -> None:
        self._known = known
        self._plan = plan
        self._chunker = FixedSizeChunker(plan, WordTokenizer())

    def plan_for(self, source_uri: str) -> ChunkPlan:
        if source_uri not in self._known:
            raise ChunkingStrategyError(f"no chunking strategy set for {source_uri!r}")
        return self._plan

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
    session: AsyncSession,
    documents: Sequence[ExtractedDocument | FakeCandidate],
    embedder: FakeEmbedder,
    *,
    plan: ChunkPlan = PLAN,
    extractor: FakeExtractor | None = None,
    limits: IngestionLimits | None = None,
) -> IngestionPipeline:
    extractor = extractor or FakeExtractor(documents)
    known = {c.source_uri for c in extractor.candidates() if c.source_uri != "nostrategy.md"}
    return IngestionPipeline(
        extractor=extractor,
        chunkers=FakeChunkers(known, plan),
        embedder=embedder,
        session_factory=lambda: session,
        vector_store_factory=PgVectorStore,
        limits=limits,
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
    assert source.source_hash == hashlib.sha256(TEXT.encode()).hexdigest()
    assert (source.source_type, source.source_uri, source.status) == ("file", "a.md", "active")
    assert source.metadata_ == {"filename": "a.md"}

    chunks = (await session.execute(select(Chunk).order_by(Chunk.chunk_index))).scalars().all()
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    assert all(c.embedding_model == "fake-embedding" for c in chunks)
    vectors = (await session.execute(select(ChunkVector))).scalars().all()
    assert {v.chunk_id for v in vectors} == {c.id for c in chunks}
    assert vectors[0].metadata_["source_uri"] == "a.md"


async def test_the_recorded_plan_holds_the_chunking_settings_and_the_reader(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    source = (await session.execute(select(Source))).scalar_one()
    assert source.chunking == {
        **PLAN.as_metadata(),
        "reader": "fake",
        "reader_version": 1,
    }


# --- change detection: what a run can decide without opening a file ----------------------------


async def test_an_unchanged_source_is_skipped_without_being_opened(session: AsyncSession) -> None:
    embedder = FakeEmbedder()
    await make_pipeline(session, [doc("a.md", TEXT)], embedder).run()
    calls = embedder.calls
    await session.commit()

    extractor = FakeExtractor([doc("a.md", TEXT)])
    report = await make_pipeline(session, [], embedder, extractor=extractor).run()

    assert [r.outcome for r in report.results] == [Outcome.SKIPPED]
    assert report.results[0].detail == "unchanged, not opened"
    assert extractor.opened == []  # the whole point: no parse at all
    assert (report.opened, report.unopened) == (0, 1)
    assert embedder.calls == calls
    assert await count(session, Chunk) == 3


async def test_a_changed_source_is_opened_and_replaces_its_chunks_and_vectors(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    old_ids = set((await session.execute(select(Chunk.id))).scalars())
    await session.commit()  # the pipeline opens its own transaction on this shared session

    extractor = FakeExtractor([doc("a.md", "just four words here")])
    report = await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert [r.outcome for r in report.results] == [Outcome.UPDATED]
    assert extractor.opened == ["a.md"]
    new_ids = set((await session.execute(select(Chunk.id))).scalars())
    assert len(new_ids) == 1
    assert not new_ids & old_ids
    assert await count(session, ChunkVector) == 1
    source = (await session.execute(select(Source))).scalar_one()
    assert source.content_hash == hashlib.sha256(b"just four words here").hexdigest()


async def test_only_the_changed_document_is_opened(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT), doc("b.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    extractor = FakeExtractor([doc("a.md", TEXT), doc("b.md", "quite different words now")])
    report = await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert extractor.opened == ["b.md"]
    assert {(r.source_uri, r.outcome) for r in report.results} == {
        ("a.md", Outcome.SKIPPED),
        ("b.md", Outcome.UPDATED),
    }
    assert (report.opened, report.unopened) == (1, 1)


async def test_a_file_whose_bytes_changed_but_text_did_not_is_not_re_embedded(
    session: AsyncSession,
) -> None:
    embedder = FakeEmbedder()
    await make_pipeline(session, [doc("a.md", TEXT)], embedder).run()
    calls = embedder.calls
    await session.commit()

    # Same extracted text, different raw bytes (a reformatted file, a changed line ending).
    extractor = FakeExtractor(
        [
            FakeCandidate(
                source_type="file",
                source_uri="a.md",
                source_hash="a-brand-new-raw-hash",
                reader="fake",
                reader_version=1,
                document=doc("a.md", TEXT),
                opened=[],
            )
        ]
    )
    report = await make_pipeline(session, [], embedder, extractor=extractor).run()

    assert [(r.outcome, r.detail) for r in report.results] == [(Outcome.SKIPPED, "text unchanged")]
    assert extractor.opened == ["a.md"]  # it had to be opened to find that out
    assert embedder.calls == calls
    # The new raw hash is recorded, so the next run skips it without opening it.
    source = (await session.execute(select(Source))).scalar_one()
    assert source.source_hash == "a-brand-new-raw-hash"


async def test_changed_chunking_settings_re_chunk_a_document_that_did_not_change(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    assert await count(session, Chunk) == 3
    await session.commit()

    bigger = replace(PLAN, chunk_size=12, chunk_overlap=0)
    extractor = FakeExtractor([doc("a.md", TEXT)])
    report = await make_pipeline(
        session, [], FakeEmbedder(), plan=bigger, extractor=extractor
    ).run()

    assert [r.outcome for r in report.results] == [Outcome.UPDATED]
    assert extractor.opened == ["a.md"]
    assert await count(session, Chunk) == 1  # 12 words now fit in one chunk
    assert await count(session, ChunkVector) == 1
    source = (await session.execute(select(Source))).scalar_one()
    assert source.chunking["chunk_size"] == 12


async def test_bumping_a_readers_version_re_extracts_the_documents_it_read(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    improved = FakeCandidate(
        source_type="file",
        source_uri="a.md",
        source_hash=hashlib.sha256(TEXT.encode()).hexdigest(),  # the file itself is untouched
        reader="fake",
        reader_version=2,
        document=doc("a.md", "the improved reader found other words"),
        opened=[],
    )
    extractor = FakeExtractor([improved])
    report = await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert [r.outcome for r in report.results] == [Outcome.UPDATED]
    assert extractor.opened == ["a.md"]
    source = (await session.execute(select(Source))).scalar_one()
    assert source.chunking["reader_version"] == 2


async def test_the_reason_a_document_was_opened_is_logged(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    with caplog.at_level(logging.INFO, logger="app.ingestion.pipeline"):
        await make_pipeline(
            session, [doc("a.md", "entirely different text here")], FakeEmbedder()
        ).run()

    assert any("opening a.md: the file changed" in r.getMessage() for r in caplog.records)


# --- re-sync: sources that have gone -----------------------------------------------------------


async def test_a_deleted_source_is_marked_deleted_and_loses_its_chunks_and_vectors(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT), doc("b.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    extractor = FakeExtractor([doc("a.md", TEXT)])  # b.md is gone from the corpus
    report = await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    removed = report.of(Outcome.REMOVED)
    assert [r.source_uri for r in removed] == ["b.md"]
    rows = (await session.execute(select(Source.source_uri, Source.status))).all()
    assert {row.source_uri: row.status for row in rows} == {"a.md": "active", "b.md": "deleted"}
    # No orphans: a.md keeps its 3 chunks, b.md keeps none.
    assert await count(session, Chunk) == 3
    assert await count(session, ChunkVector) == 3


async def test_a_source_already_marked_deleted_is_not_removed_again(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()
    await make_pipeline(session, [], FakeEmbedder(), extractor=FakeExtractor([])).run(
        allow_mass_delete=True
    )
    await session.commit()

    report = await make_pipeline(session, [], FakeEmbedder(), extractor=FakeExtractor([])).run()

    assert report.results == []


async def test_a_deleted_source_that_comes_back_is_ingested_again(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()
    await make_pipeline(session, [], FakeEmbedder(), extractor=FakeExtractor([])).run(
        allow_mass_delete=True
    )
    await session.commit()

    extractor = FakeExtractor([doc("a.md", TEXT)])
    report = await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert [r.outcome for r in report.results] == [Outcome.UPDATED]
    assert extractor.opened == ["a.md"]  # the raw hash matches, but it was not active
    source = (await session.execute(select(Source))).scalar_one()
    assert source.status == "active"
    assert await count(session, Chunk) == 3


# --- failures -----------------------------------------------------------------------------------


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


async def test_an_unreadable_source_fails_and_is_not_treated_as_deleted(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    unreadable = FakeCandidate(
        source_type="file",
        source_uri="a.md",
        source_hash="changed",
        reader="fake",
        reader_version=1,
        document=None,
        opened=[],
    )
    report = await make_pipeline(
        session, [], FakeEmbedder(), extractor=FakeExtractor([unreadable])
    ).run()

    assert [(r.source_uri, r.outcome) for r in report.results] == [("a.md", Outcome.FAILED)]
    source = (await session.execute(select(Source))).scalar_one()
    assert source.status == "active"  # still there, just not re-read
    assert await count(session, Chunk) == 3


async def test_document_without_a_chunking_strategy_fails_without_being_opened(
    session: AsyncSession,
) -> None:
    documents = [doc("nostrategy.md", TEXT), doc("good.md", TEXT)]
    extractor = FakeExtractor(documents)

    report = await make_pipeline(session, documents, FakeEmbedder(), extractor=extractor).run()

    assert [(r.source_uri, r.outcome) for r in report.results] == [
        ("nostrategy.md", Outcome.FAILED),
        ("good.md", Outcome.ADDED),
    ]
    assert "no chunking strategy" in report.results[0].detail
    assert extractor.opened == ["good.md"]
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

    with caplog.at_level(logging.INFO, logger="app.ingestion.pipeline"):
        now[0] = 30.0
        progress.advance(50)  # 25% in 30s -> 90s left
        now[0] = 120.0
        progress.advance(150)  # done: no estimate

    assert [r.getMessage() for r in caplog.records] == [
        "processed 50/200 chunks (25%), elapsed 30s, ~1m 30s remaining",
        "processed 200/200 chunks (100%), elapsed 2m 00s",
    ]


async def test_run_logs_what_it_opened_and_what_it_did_not(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    with caplog.at_level(logging.INFO, logger="app.ingestion.pipeline"):
        report = await make_pipeline(
            session, [], FakeEmbedder(), extractor=FakeExtractor([doc("a.md", TEXT)])
        ).run()

    messages = [r.getMessage() for r in caplog.records]
    assert "0 sources need opening; 1 are unchanged and will not be parsed" in messages
    assert report.opening_summary() == "opened 0 of 1 sources; 1 never had to be opened"


# --- limits: a costly document is rejected with a named reason, and the run carries on ----------


class RejectedCandidate(FakeCandidate):
    def read(self) -> ExtractedDocument:
        self.opened.append(self.source_uri)
        raise LimitExceededError("file_too_large", f"{self.source_uri}: too big")


@pytest.mark.safety_limits
async def test_a_rejected_file_is_reported_by_name_and_the_run_continues(
    session: AsyncSession,
) -> None:
    rejected = RejectedCandidate(
        source_type="file",
        source_uri="huge.md",
        source_hash="",
        reader="fake",
        reader_version=1,
        document=None,
        opened=[],
    )
    report = await make_pipeline(session, [rejected, doc("a.md", TEXT)], FakeEmbedder()).run()

    outcomes = {r.source_uri: (r.outcome, r.detail) for r in report.results}
    assert outcomes["huge.md"][0] == Outcome.FAILED
    assert "rejected (file_too_large)" in outcomes["huge.md"][1]
    assert outcomes["a.md"][0] == Outcome.ADDED


@pytest.mark.safety_limits
@pytest.mark.parametrize(
    ("limits", "document", "reason"),
    [
        (IngestionLimits(max_extracted_chars=20), doc("a.md", TEXT), "too_much_text"),
        (IngestionLimits(max_chunks_per_document=2), doc("a.md", TEXT), "too_many_chunks"),
        (
            IngestionLimits(max_metadata_bytes=100),
            ExtractedDocument("file", "a.md", TEXT, {"front_matter": "x" * 200}),
            "metadata_too_large",
        ),
    ],
)
async def test_a_document_over_a_limit_is_not_embedded(
    session: AsyncSession, limits: IngestionLimits, document: ExtractedDocument, reason: str
) -> None:
    embedder = FakeEmbedder()
    report = await make_pipeline(session, [document], embedder, limits=limits).run()

    (result,) = report.results
    assert result.outcome == Outcome.FAILED
    assert f"rejected ({reason})" in result.detail
    assert embedder.calls == 0
    assert await count(session, Source) == 0


@pytest.mark.safety_deletion
async def test_a_run_that_would_empty_the_corpus_stops_before_doing_anything(
    session: AsyncSession,
) -> None:
    await make_pipeline(session, [doc("a.md", TEXT), doc("b.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    extractor = FakeExtractor([])  # an empty mount looks exactly like this
    with pytest.raises(MassDeletionError, match="2 of 2"):
        await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert await count(session, Chunk) == 6  # nothing was deleted
    sources = (await session.execute(select(Source))).scalars().all()
    assert {s.status for s in sources} == {"active"}


@pytest.mark.safety_deletion
async def test_a_run_that_would_delete_too_much_opens_nothing_either(
    session: AsyncSession,
) -> None:
    await make_pipeline(
        session, [doc("a.md", TEXT), doc("b.md", TEXT), doc("c.md", TEXT)], FakeEmbedder()
    ).run()
    await session.commit()

    extractor = FakeExtractor([doc("a.md", "changed text")])  # 2 of 3 gone
    with pytest.raises(MassDeletionError):
        await make_pipeline(session, [], FakeEmbedder(), extractor=extractor).run()

    assert extractor.opened == []


@pytest.mark.safety_deletion
async def test_allow_mass_delete_lets_one_run_empty_the_corpus(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT), doc("b.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    report = await make_pipeline(session, [], FakeEmbedder(), extractor=FakeExtractor([])).run(
        allow_mass_delete=True
    )

    assert [r.outcome for r in report.results] == [Outcome.REMOVED, Outcome.REMOVED]
    assert await count(session, Chunk) == 0


@pytest.mark.safety_deletion
async def test_deleting_within_the_fraction_needs_no_override(session: AsyncSession) -> None:
    await make_pipeline(session, [doc("a.md", TEXT), doc("b.md", TEXT)], FakeEmbedder()).run()
    await session.commit()

    report = await make_pipeline(
        session, [], FakeEmbedder(), extractor=FakeExtractor([doc("a.md", TEXT)])
    ).run()

    assert [r.source_uri for r in report.of(Outcome.REMOVED)] == ["b.md"]
