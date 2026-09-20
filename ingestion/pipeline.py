"""Orchestrates extract -> chunk -> embed -> store for every document in a source.

Each document is written in its own transaction, so a failure leaves that document untouched
(and the rest of the run unaffected). Postgres rows and vectors are written together.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from chunking.base import Chunker
from db.models import SourceStatus
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository
from embedding.base import EmbeddingError, EmbeddingProvider
from extraction.base import ExtractedDocument, Extractor
from vector_store.base import VectorRecord, VectorStore

logger = logging.getLogger(__name__)

VectorStoreFactory = Callable[[AsyncSession], VectorStore]
SessionFactory = Callable[[], AsyncSession]


class Outcome(StrEnum):
    ADDED = "added"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


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


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestionPipeline:
    def __init__(
        self,
        *,
        extractor: Extractor,
        chunker: Chunker,
        embedder: EmbeddingProvider,
        session_factory: SessionFactory,
        vector_store_factory: VectorStoreFactory,
    ) -> None:
        self._extractor = extractor
        self._chunker = chunker
        self._embedder = embedder
        self._session_factory = session_factory
        self._vector_store_factory = vector_store_factory

    async def run(self) -> IngestionReport:
        # Extractors are synchronous (file/network readers); keep them off the event loop.
        documents = await asyncio.to_thread(lambda: list(self._extractor.extract()))
        report = IngestionReport()
        for document in documents:
            report.results.append(await self._ingest_safely(document))
        return report

    async def _ingest_safely(self, document: ExtractedDocument) -> DocumentResult:
        try:
            return await self.ingest_document(document)
        except (EmbeddingError, ValueError) as exc:
            logger.warning("failed to ingest %s: %s", document.source_uri, exc)
            return DocumentResult(document.source_uri, Outcome.FAILED, str(exc))
        except Exception:
            logger.exception("unexpected error ingesting %s", document.source_uri)
            return DocumentResult(document.source_uri, Outcome.FAILED, "unexpected error")

    async def ingest_document(self, document: ExtractedDocument) -> DocumentResult:
        uri = document.source_uri
        content_hash = hash_content(document.text)
        chunks = self._chunker.chunk(document.text)
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
                return DocumentResult(uri, Outcome.SKIPPED, "unchanged")

            vectors = await self._embedder.embed([c.text for c in chunks])
            if len(vectors) != len(chunks):
                raise EmbeddingError(f"expected {len(chunks)} embeddings, got {len(vectors)}")

            vector_store = self._vector_store_factory(session)
            chunk_repo = ChunkRepository(session)
            if existing is None:
                source = await sources.create(
                    source_type=document.source_type,
                    source_uri=uri,
                    content_hash=content_hash,
                    metadata=document.metadata,
                )
            else:
                # Changed content: drop the stale chunks and vectors in the same transaction.
                source = existing
                await vector_store.delete_by_source(source.id)
                await chunk_repo.delete_by_source(source.id)
                await sources.mark_ingested(
                    source, content_hash=content_hash, metadata=document.metadata
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
            await vector_store.upsert(
                [
                    VectorRecord(
                        chunk_id=chunk_id,
                        embedding=vector,
                        source_id=source.id,
                        source_type=document.source_type,
                        chunk_index=chunk.index,
                        metadata={
                            "source_uri": uri,
                            "char_start": chunk.char_start,
                            "char_end": chunk.char_end,
                        },
                    )
                    for chunk_id, chunk, vector in zip(chunk_ids, chunks, vectors, strict=True)
                ]
            )

        outcome = Outcome.ADDED if existing is None else Outcome.UPDATED
        return DocumentResult(uri, outcome, f"{len(chunks)} chunks")
