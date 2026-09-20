"""Initial schema: sources, chunks, chunk_vectors, api_keys, query_logs.

Revision ID: 0001
Revises:
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here on purpose: a migration must not change when db.models.EMBEDDING_DIMENSIONS does.
EMBEDDING_DIMENSIONS = 1536

_NOW = sa.text("now()")
_NEW_UUID = sa.text("gen_random_uuid()")
_EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=_NEW_UUID, nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "last_ingested_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default=_EMPTY_JSON, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_uri", name="uq_sources_type_uri"),
        sa.CheckConstraint("status IN ('active', 'stale', 'deleted')", name="ck_sources_status"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=_NEW_UUID, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "chunk_index", name="uq_chunks_source_index"),
    )

    op.create_table(
        "chunk_vectors",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default=_EMPTY_JSON, nullable=False),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_chunk_vectors_source_id", "chunk_vectors", ["source_id"])
    op.create_index("ix_chunk_vectors_source_type", "chunk_vectors", ["source_type"])
    op.create_index(
        "ix_chunk_vectors_embedding_hnsw",
        "chunk_vectors",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=_NEW_UUID, nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )

    op.create_table(
        "query_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=_NEW_UUID, nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "retrieved_chunk_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"]),
    )
    op.create_index("ix_query_logs_api_key_id", "query_logs", ["api_key_id"])


def downgrade() -> None:
    op.drop_table("query_logs")
    op.drop_table("api_keys")
    op.drop_table("chunk_vectors")
    op.drop_table("chunks")
    op.drop_table("sources")
    # The vector extension is left installed: it may be shared with other schemas/databases users.
