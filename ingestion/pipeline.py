"""Orchestrates extract -> chunk -> embed -> store for every document in a source.

A run enumerates the sources that exist (cheap: a hash of each one's raw bytes) and compares them
against what is stored, so a document that has not changed is never opened. Only the ones that
have to be opened are parsed, chunked, embedded and stored, one document at a time and one
transaction each: a failure leaves that document untouched and the rest of the run unaffected,
and nothing more than a single document's chunks is ever held in memory.

Sources that no longer appear are marked deleted, and their chunks and vectors go with them.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from chunking.base import ChunkerResolver, ChunkingStrategyError, ChunkPlan, TextChunk
from db.models import JsonObject, SourceStatus
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository, SourceState
from embedding.base import EmbeddingError, EmbeddingProvider
from extraction.base import ExtractedDocument, ExtractionError, Extractor, SourceCandidate
from ingestion.changes import plan_record, reason_to_open
from vector_store.base import VectorRecord, VectorStore

logger = logging.getLogger(__name__)

VectorStoreFactory = Callable[[AsyncSession], VectorStore]
VECTOR_WRITE_BATCH_SIZE = 500  # vectors written per statement; also the granularity of logging
EMBED_BATCH_SIZE = 96  # chunks per embedding request; also the granularity of progress logging
SessionFactory = Callable[[], AsyncSession]


class Outcome(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    SKIPPED = "skipped"
    REMOVED = "removed"
    FAILED = "failed"


@dataclass(frozen=True)
class PreparedDocument:
    """A document's chunks and the plan they were made with, ready to store."""

    plan: ChunkPlan
    chunks: list[TextChunk]

    def __len__(self) -> int:
        return len(self.chunks)


@dataclass(frozen=True)
class Pending:
    """A source that has to be opened, and why."""

    candidate: SourceCandidate
    plan: ChunkPlan
    record: JsonObject  # what to store in sources.chunking once it is ingested
    reason: str


@dataclass(frozen=True)
class DocumentResult:
    source_uri: str
    outcome: Outcome
    detail: str = ""


@dataclass
class IngestionReport:
    results: list[DocumentResult] = field(default_factory=list)
    opened: int = 0  # sources parsed this run
    unopened: int = 0  # sources skipped on their raw hash, without being parsed

    def of(self, outcome: Outcome) -> list[DocumentResult]:
        return [r for r in self.results if r.outcome == outcome]

    def counts(self) -> str:
        return ", ".join(f"{len(self.of(o))} {o.value}" for o in Outcome)

    def opening_summary(self) -> str:
        total = self.opened + self.unopened
        return f"opened {self.opened} of {total} sources; {self.unopened} never had to be opened"


def log_chunks(uri: str, plan: ChunkPlan, chunks: Sequence[TextChunk]) -> None:
    """INFO: per-document chunking summary. DEBUG: every chunk with its metadata and a preview."""
    if not chunks:
        logger.info("chunked %s [%s]: no chunks (no text content)", uri, plan.describe())
        return
    sizes = [c.token_count for c in chunks]
    types = Counter(str(c.metadata.get("chunk_type", "text")) for c in chunks)
    logger.info(
        "chunked %s [%s]: %d chunks, tokens min/avg/max = %d/%d/%d, types: %s",
        uri,
        plan.describe(),
        len(chunks),
        min(sizes),
        sum(sizes) // len(sizes),
        max(sizes),
        ", ".join(f"{name}={count}" for name, count in sorted(types.items())),
    )
    if logger.isEnabledFor(logging.DEBUG):
        for chunk in chunks:
            preview = " ".join(chunk.text.split())[:100]
            logger.debug(
                "  %s chunk %d: %d tokens, chars %d-%d, meta=%s | %s",
                uri,
                chunk.index,
                chunk.token_count,
                chunk.char_start,
                chunk.char_end,
                chunk.metadata,
                preview,
            )


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


class ProgressTracker:
    """Logs how far through the run's work it is, with a time-remaining estimate."""

    def __init__(
        self, total: int, unit: str = "chunks", clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.total = total
        self.unit = unit
        self.done = 0
        self._clock = clock
        self._started = clock()

    def advance(self, count: int, *, log: bool = True) -> None:
        self.done += count
        if not log or self.total == 0:
            return
        elapsed = self._clock() - self._started
        remaining = ""
        if 0 < self.done < self.total and elapsed > 0:
            eta = elapsed / self.done * (self.total - self.done)
            remaining = f", ~{format_duration(eta)} remaining"
        logger.info(
            "processed %d/%d %s (%d%%), elapsed %s%s",
            self.done,
            self.total,
            self.unit,
            self.done * 100 // self.total,
            format_duration(elapsed),
            remaining,
        )


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestionPipeline:
    def __init__(
        self,
        *,
        extractor: Extractor,
        chunkers: ChunkerResolver,
        embedder: EmbeddingProvider,
        session_factory: SessionFactory,
        vector_store_factory: VectorStoreFactory,
    ) -> None:
        self._extractor = extractor
        self._chunkers = chunkers
        self._embedder = embedder
        self._session_factory = session_factory
        self._vector_store_factory = vector_store_factory

    async def run(self) -> IngestionReport:
        source_type = self._extractor.source_type
        started = time.monotonic()
        # Enumeration is synchronous (it hashes files); keep it off the event loop.
        candidates = await asyncio.to_thread(lambda: list(self._extractor.candidates()))
        async with self._session_factory() as session:
            stored = await SourceRepository(session).state_for_type(source_type)
        logger.info(
            "found %d sources (%d already stored), hashed in %s",
            len(candidates),
            len(stored),
            format_duration(time.monotonic() - started),
        )

        report = IngestionReport()
        pending = self._decide(candidates, stored, report)
        logger.info(
            "%d sources need opening; %d are unchanged and will not be parsed",
            len(pending),
            report.unopened,
        )

        run_started = time.monotonic()
        progress = ProgressTracker(len(pending), unit="documents")
        for item in pending:
            report.results.append(await self._process(item))
            report.opened += 1
            progress.advance(1)
        report.results.extend(
            await self._remove_missing(source_type, stored, {c.source_uri for c in candidates})
        )
        logger.info(
            "ingestion finished in %s: %s (%s)",
            format_duration(time.monotonic() - run_started),
            report.counts(),
            report.opening_summary(),
        )
        return report

    def _decide(
        self,
        candidates: Sequence[SourceCandidate],
        stored: dict[str, SourceState],
        report: IngestionReport,
    ) -> list[Pending]:
        """Split the candidates into the ones worth opening and the ones that are unchanged.

        Nothing here parses a file: the decision rests on the raw hash and the plan recorded at
        the last ingestion, which is the whole point of enumerating candidates lazily.
        """
        pending: list[Pending] = []
        for candidate in candidates:
            uri = candidate.source_uri
            try:
                plan = self._chunkers.plan_for(uri)
            except ChunkingStrategyError as exc:
                report.results.append(DocumentResult(uri, Outcome.FAILED, str(exc)))
                continue
            record = plan_record(plan, candidate)
            reason = reason_to_open(stored.get(uri), candidate, record)
            if reason is None:
                report.unopened += 1
                report.results.append(DocumentResult(uri, Outcome.SKIPPED, "unchanged, not opened"))
                continue
            pending.append(Pending(candidate, plan, record, reason))
        return pending

    async def _process(self, item: Pending) -> DocumentResult:
        uri = item.candidate.source_uri
        logger.info("opening %s: %s", uri, item.reason)
        try:
            document = await asyncio.to_thread(item.candidate.read)
        except ExtractionError as exc:
            logger.warning("failed to read %s: %s", uri, exc)
            return DocumentResult(uri, Outcome.FAILED, str(exc))
        try:
            prepared = self._chunk(document, item.plan)
        except ValueError as exc:
            logger.warning("failed to chunk %s: %s", uri, exc)
            return DocumentResult(uri, Outcome.FAILED, str(exc))
        try:
            return await self.ingest_document(document, prepared, item)
        except (EmbeddingError, ValueError) as exc:
            logger.warning("failed to ingest %s: %s", uri, exc)
            return DocumentResult(uri, Outcome.FAILED, str(exc))
        except Exception:
            logger.exception("unexpected error ingesting %s", uri)
            return DocumentResult(uri, Outcome.FAILED, "unexpected error")

    def _chunk(self, document: ExtractedDocument, plan: ChunkPlan) -> PreparedDocument:
        uri = document.source_uri
        chunks = self._chunkers.for_document(uri).chunk(document)
        log_chunks(uri, plan, chunks)
        return PreparedDocument(plan, chunks)

    async def ingest_document(
        self, document: ExtractedDocument, prepared: PreparedDocument, item: Pending
    ) -> DocumentResult:
        uri = document.source_uri
        chunks = prepared.chunks
        content_hash = hash_content(document.text)
        source_hash = item.candidate.source_hash
        if not chunks:
            return DocumentResult(uri, Outcome.SKIPPED, "no text content")

        async with self._session_factory() as session, session.begin():
            sources = SourceRepository(session)
            existing = await sources.get_by_uri(document.source_type, uri)
            if (
                existing is not None
                and existing.content_hash == content_hash
                and existing.chunking == item.record
                and existing.status == SourceStatus.ACTIVE.value
            ):
                # The file's bytes changed but the text they produce did not: record the new raw
                # hash so the next run skips it without opening it, and re-embed nothing.
                logger.info("skipping %s: its extracted text is unchanged", uri)
                await sources.mark_ingested(
                    existing,
                    content_hash=content_hash,
                    source_hash=source_hash,
                    metadata=document.metadata,
                    chunking=item.record,
                )
                return DocumentResult(uri, Outcome.SKIPPED, "text unchanged")

            logger.info(
                "%s %s: embedding %d chunks with %s",
                "updating" if existing else "adding",
                uri,
                len(chunks),
                self._embedder.model_name,
            )
            vectors: list[list[float]] = []
            for start in range(0, len(chunks), EMBED_BATCH_SIZE):
                batch = chunks[start : start + EMBED_BATCH_SIZE]
                batch_vectors = await self._embedder.embed([c.text for c in batch])
                if len(batch_vectors) != len(batch):
                    raise EmbeddingError(
                        f"expected {len(batch)} embeddings, got {len(batch_vectors)}"
                    )
                vectors.extend(batch_vectors)
                logger.info("%s: embedded %d/%d chunks", uri, len(vectors), len(chunks))

            write_started = time.monotonic()
            logger.info("writing %s to Postgres (%d chunks)", uri, len(chunks))
            vector_store = self._vector_store_factory(session)
            chunk_repo = ChunkRepository(session)
            if existing is None:
                source = await sources.create(
                    source_type=document.source_type,
                    source_uri=uri,
                    content_hash=content_hash,
                    source_hash=source_hash,
                    metadata=document.metadata,
                    chunking=item.record,
                )
            else:
                # Changed content: drop the stale chunks and vectors in the same transaction.
                source = existing
                await vector_store.delete_by_source(source.id)
                await chunk_repo.delete_by_source(source.id)
                await sources.mark_ingested(
                    source,
                    content_hash=content_hash,
                    source_hash=source_hash,
                    metadata=document.metadata,
                    chunking=item.record,
                )

            chunk_ids = [uuid.uuid4() for _ in chunks]
            await chunk_repo.add_many(
                [
                    NewChunk(
                        id=chunk_id,
                        source_id=source.id,
                        chunk_index=chunk.index,
                        text=chunk.text,
                        token_count=chunk.token_count,
                        embedding_model=self._embedder.model_name,
                    )
                    for chunk_id, chunk in zip(chunk_ids, chunks, strict=True)
                ]
            )
            logger.info("%s: stored %d chunk rows, storing vectors", uri, len(chunks))
            records = [
                VectorRecord(
                    chunk_id=chunk_id,
                    embedding=vector,
                    source_id=source.id,
                    source_type=document.source_type,
                    chunk_index=chunk.index,
                    metadata={
                        **chunk.metadata,
                        "source_uri": uri,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                    },
                )
                for chunk_id, chunk, vector in zip(chunk_ids, chunks, vectors, strict=True)
            ]
            for start in range(0, len(records), VECTOR_WRITE_BATCH_SIZE):
                await vector_store.upsert(records[start : start + VECTOR_WRITE_BATCH_SIZE])
                written = min(start + VECTOR_WRITE_BATCH_SIZE, len(records))
                logger.info("%s: stored vectors %d/%d", uri, written, len(records))
            logger.info("%s: committing transaction", uri)

        logger.info(
            "%s: committed %d chunks (write took %s)",
            uri,
            len(chunks),
            format_duration(time.monotonic() - write_started),
        )

        outcome = Outcome.ADDED if existing is None else Outcome.UPDATED
        return DocumentResult(uri, outcome, f"{len(chunks)} chunks")

    async def _remove_missing(
        self, source_type: str, stored: dict[str, SourceState], present: set[str]
    ) -> list[DocumentResult]:
        """Mark sources that no longer exist `deleted`, and remove their chunks and vectors.

        The candidate list is exactly the set of sources that still exist, so anything stored and
        not already deleted that is missing from it has gone. Each removal is its own
        transaction, so Postgres rows and vectors can never be left half-deleted.
        """
        missing = sorted(
            uri
            for uri, state in stored.items()
            if uri not in present and state.status != SourceStatus.DELETED.value
        )
        results: list[DocumentResult] = []
        for uri in missing:
            try:
                results.append(await self._remove(source_type, uri))
            except Exception:
                logger.exception("failed to remove %s", uri)
                results.append(DocumentResult(uri, Outcome.FAILED, "removal failed"))
        return results

    async def _remove(self, source_type: str, uri: str) -> DocumentResult:
        async with self._session_factory() as session, session.begin():
            sources = SourceRepository(session)
            source = await sources.get_by_uri(source_type, uri)
            if source is None:  # removed by something else since the state was loaded
                return DocumentResult(uri, Outcome.REMOVED, "already gone")
            vectors = await self._vector_store_factory(session).delete_by_source(source.id)
            chunks = await ChunkRepository(session).delete_by_source(source.id)
            await sources.set_status(source, SourceStatus.DELETED)
        logger.info("%s no longer exists: marked deleted, removed %d chunks", uri, chunks)
        return DocumentResult(uri, Outcome.REMOVED, f"{chunks} chunks and {vectors} vectors")
