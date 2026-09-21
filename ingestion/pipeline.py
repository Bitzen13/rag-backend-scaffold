"""Orchestrates extract -> chunk -> embed -> store for every document in a source.

Each document is written in its own transaction, so a failure leaves that document untouched
(and the rest of the run unaffected). Postgres rows and vectors are written together.
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

from chunking.base import ChunkerResolver, ChunkPlan, TextChunk
from db.models import SourceStatus
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository
from embedding.base import EmbeddingError, EmbeddingProvider
from extraction.base import ExtractedDocument, Extractor
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
    FAILED = "failed"


@dataclass(frozen=True)
class PreparedDocument:
    """A document's chunks and the plan they were made with, ready to store."""

    plan: ChunkPlan
    chunks: list[TextChunk]

    def __len__(self) -> int:
        return len(self.chunks)


@dataclass(frozen=True)
class DocumentResult:
    source_uri: str
    outcome: Outcome
    detail: str = ""


@dataclass
class IngestionReport:
    results: list[DocumentResult] = field(default_factory=list)

    def of(self, outcome: Outcome) -> list[DocumentResult]:
        return [r for r in self.results if r.outcome == outcome]


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
    """Logs how many chunks are processed across the whole run, with a time-remaining estimate."""

    def __init__(self, total: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.total = total
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
            "processed %d/%d chunks (%d%%), elapsed %s%s",
            self.done,
            self.total,
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
        # Extractors are synchronous (file/network readers); keep them off the event loop.
        logger.info("extracting documents (large PDFs can take a minute or two)")
        started = time.monotonic()
        documents = await asyncio.to_thread(lambda: list(self._extractor.extract()))
        logger.info(
            "extracted %d documents in %s",
            len(documents),
            format_duration(time.monotonic() - started),
        )

        # Chunk everything first so progress can report against the total chunk count.
        prepared: list[tuple[ExtractedDocument, PreparedDocument | DocumentResult]] = []
        for document in documents:
            try:
                prepared.append((document, self._chunk(document)))
            except ValueError as exc:
                logger.warning("failed to chunk %s: %s", document.source_uri, exc)
                prepared.append(
                    (document, DocumentResult(document.source_uri, Outcome.FAILED, str(exc)))
                )
        progress = ProgressTracker(
            sum(len(p.chunks) for _, p in prepared if isinstance(p, PreparedDocument))
        )
        logger.info("chunked %d documents into %d chunks", len(documents), progress.total)

        report = IngestionReport()
        run_started = time.monotonic()
        for document, outcome in prepared:
            if isinstance(outcome, DocumentResult):
                report.results.append(outcome)
            else:
                report.results.append(await self._ingest_safely(document, outcome, progress))
        logger.info(
            "ingestion finished in %s: %s",
            format_duration(time.monotonic() - run_started),
            ", ".join(f"{len(report.of(o))} {o.value}" for o in Outcome),
        )
        return report

    def _chunk(self, document: ExtractedDocument) -> PreparedDocument:
        uri = document.source_uri
        plan = self._chunkers.plan_for(uri)
        chunks = self._chunkers.for_document(uri).chunk(document)
        log_chunks(uri, plan, chunks)
        return PreparedDocument(plan, chunks)

    async def _ingest_safely(
        self, document: ExtractedDocument, prepared: PreparedDocument, progress: ProgressTracker
    ) -> DocumentResult:
        try:
            return await self.ingest_document(document, prepared, progress)
        except (EmbeddingError, ValueError) as exc:
            logger.warning("failed to ingest %s: %s", document.source_uri, exc)
            return DocumentResult(document.source_uri, Outcome.FAILED, str(exc))
        except Exception:
            logger.exception("unexpected error ingesting %s", document.source_uri)
            return DocumentResult(document.source_uri, Outcome.FAILED, "unexpected error")

    async def ingest_document(
        self, document: ExtractedDocument, prepared: PreparedDocument, progress: ProgressTracker
    ) -> DocumentResult:
        uri = document.source_uri
        chunks = prepared.chunks
        content_hash = hash_content(document.text)
        if not chunks:
            return DocumentResult(uri, Outcome.SKIPPED, "no text content")

        async with self._session_factory() as session, session.begin():
            sources = SourceRepository(session)
            existing = await sources.get_by_uri(document.source_type, uri)
            if (
                existing is not None
                and existing.content_hash == content_hash
                and existing.status == SourceStatus.ACTIVE.value
            ):
                logger.info("skipping %s: content unchanged since last ingestion", uri)
                progress.advance(len(chunks), log=False)
                return DocumentResult(uri, Outcome.SKIPPED, "unchanged")

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
                progress.advance(len(batch))

            write_started = time.monotonic()
            logger.info("writing %s to Postgres (%d chunks)", uri, len(chunks))
            vector_store = self._vector_store_factory(session)
            chunk_repo = ChunkRepository(session)
            if existing is None:
                source = await sources.create(
                    source_type=document.source_type,
                    source_uri=uri,
                    content_hash=content_hash,
                    metadata=document.metadata,
                    chunking=prepared.plan.as_metadata(),
                )
            else:
                # Changed content: drop the stale chunks and vectors in the same transaction.
                source = existing
                await vector_store.delete_by_source(source.id)
                await chunk_repo.delete_by_source(source.id)
                await sources.mark_ingested(
                    source,
                    content_hash=content_hash,
                    metadata=document.metadata,
                    chunking=prepared.plan.as_metadata(),
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
                stored = min(start + VECTOR_WRITE_BATCH_SIZE, len(records))
                logger.info("%s: stored vectors %d/%d", uri, stored, len(records))
            logger.info("%s: committing transaction", uri)

        logger.info(
            "%s: committed %d chunks (write took %s)",
            uri,
            len(chunks),
            format_duration(time.monotonic() - write_started),
        )

        outcome = Outcome.ADDED if existing is None else Outcome.UPDATED
        return DocumentResult(uri, outcome, f"{len(chunks)} chunks")
