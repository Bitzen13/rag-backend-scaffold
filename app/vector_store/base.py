"""Vector store contract. Implementations are selected from `vector_db.provider` in config.yaml."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.db.models import JsonObject


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: uuid.UUID  # equals chunks.id in Postgres
    embedding: Sequence[float]
    source_id: uuid.UUID
    source_type: str
    chunk_index: int
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class VectorFilter:
    source_type: str | None = None


@dataclass(frozen=True)
class VectorMatch:
    chunk_id: uuid.UUID
    score: float  # cosine similarity in [-1, 1]; higher is more similar


class VectorStore(Protocol):
    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Insert or replace vectors by chunk_id."""
        ...

    async def search(
        self, embedding: Sequence[float], top_k: int, filters: VectorFilter | None = None
    ) -> list[VectorMatch]:
        """Return up to top_k nearest chunks, best first. Thresholding is the caller's job."""
        ...

    async def delete(self, chunk_ids: Sequence[uuid.UUID]) -> int:
        """Remove vectors by chunk_id; returns how many were removed."""
        ...

    async def delete_by_source(self, source_id: uuid.UUID) -> int:
        """Remove every vector belonging to a source; returns how many were removed."""
        ...
