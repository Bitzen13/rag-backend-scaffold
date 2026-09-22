import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from db.models import Base
from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "sources",
    "chunks",
    "chunk_vectors",
    "api_keys",
    "api_key_rate_limits",
    "ingestion_jobs",
    "query_logs",
    "alembic_version",
}


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        names = {row[0] for row in result}
    await engine.dispose()
    return names


async def test_upgrade_head_builds_full_schema(session: AsyncSession) -> None:
    tables = {
        row[0]
        for row in await session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
    }
    assert tables == EXPECTED_TABLES
    extension = await session.scalar(
        text("SELECT extname FROM pg_extension WHERE extname='vector'")
    )
    assert extension == "vector"


async def test_hnsw_index_exists(session: AsyncSession) -> None:
    definition = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunk_vectors_embedding_hnsw'")
    )
    assert definition is not None
    assert "hnsw" in definition
    assert "vector_cosine_ops" in definition


def test_downgrade_then_upgrade_round_trips(migrated_database_url: str) -> None:
    # Sync on purpose: Alembic's async env.py calls asyncio.run(), which needs no running loop.
    config = alembic_config(migrated_database_url)
    command.downgrade(config, "base")
    assert asyncio.run(_tables(migrated_database_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert asyncio.run(_tables(migrated_database_url)) == EXPECTED_TABLES


async def test_models_match_migrated_schema(session: AsyncSession) -> None:
    """Guards against editing db/models.py without adding a migration."""

    def diff(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    connection = await session.connection()
    assert await connection.run_sync(diff) == []


async def test_trigram_index_exists(session: AsyncSession) -> None:
    definition = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_text_trgm'")
    )
    assert definition is not None
    assert "gin_trgm_ops" in definition
