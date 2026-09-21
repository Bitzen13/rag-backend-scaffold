"""SQLAlchemy models for the Postgres schema described in architecture-context.md."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Width of the pgvector `embedding` column. It is fixed by the migration, so it must match the
# configured embedding model's output size (text-embedding-3-small = 1536). Changing the model to
# one with a different size requires a new migration that rebuilds chunk_vectors.
EMBEDDING_DIMENSIONS = 1536

JsonObject: TypeAlias = dict[str, object]


class Base(DeclarativeBase):
    pass


class SourceStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    DELETED = "deleted"


_NOW = text("now()")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("source_type", "source_uri", name="uq_sources_type_uri"),
        CheckConstraint("status IN ('active', 'stale', 'deleted')", name="ck_sources_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(Text)
    source_uri: Mapped[str] = mapped_column(Text)
    # Two hashes, because they answer different questions. `source_hash` is of the source's raw
    # bytes and gates "must this file be opened at all"; `content_hash` is of the extracted text
    # and gates "did opening it produce something different, so it needs re-embedding".
    source_hash: Mapped[str] = mapped_column(Text, default="", server_default="")
    content_hash: Mapped[str] = mapped_column(Text)
    last_ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
    status: Mapped[str] = mapped_column(
        Text, default=SourceStatus.ACTIVE.value, server_default=SourceStatus.ACTIVE.value
    )
    metadata_: Mapped[JsonObject] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # The chunking plan this source was last ingested with (strategy, sizes, splitting mode,
    # tokenizer). Phase 6 compares it against config to decide whether to re-chunk.
    chunking: Mapped[JsonObject] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uq_chunks_source_index"),
        # Trigram index: typo-tolerant word matching against chunk text (see fuzzy_search).
        Index(
            "ix_chunks_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding_model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)


class ChunkVector(Base):
    """pgvector implementation of the vector store; chunk_id joins to chunks.id."""

    __tablename__ = "chunk_vectors"
    __table_args__ = (
        Index("ix_chunk_vectors_source_id", "source_id"),
        Index("ix_chunk_vectors_source_type", "source_type"),
        Index(
            "ix_chunk_vectors_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer)
    metadata_: Mapped[JsonObject] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryLog(Base):
    __tablename__ = "query_logs"
    __table_args__ = (Index("ix_query_logs_api_key_id", "api_key_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default=text("'{}'::uuid[]")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_NOW)
