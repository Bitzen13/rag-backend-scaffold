"""Fixtures for tests that need a real Postgres (started with `docker compose up -d`).

A dedicated test database is (re)built from scratch with `alembic upgrade head` once per test
session. Each test then runs inside a transaction that is rolled back, so tests stay isolated.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from db.session import to_async_url

DEFAULT_TEST_DATABASE_URL = "postgresql://rag:rag@localhost:5432/rag_test"


def _test_database_url() -> str:
    return to_async_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))


async def _reset_database(url: str) -> None:
    """Create the test database if needed and wipe its public schema."""
    target = make_url(url)
    admin = create_async_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    await admin.dispose()

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with engine.connect() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    url = _test_database_url()
    try:
        asyncio.run(_reset_database(url))
    except (OSError, SQLAlchemyError) as exc:
        pytest.skip(f"Postgres not reachable at {url!r} (run `docker compose up -d`): {exc}")
    command.upgrade(alembic_config(url), "head")
    return url


@pytest_asyncio.fixture
async def session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real, committing sessions, for code that owns its own transactions.

    The `session` fixture below rolls everything back, which is what most tests want. Anything
    that commits for itself — the ingestion job service, whose whole point is that its writes
    outlive the request — needs sessions that really commit, and its rows cleaned up afterwards.
    """
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with factory() as cleanup, cleanup.begin():
            await cleanup.execute(text("DELETE FROM ingestion_jobs"))
        await engine.dispose()


@pytest_asyncio.fixture
async def session(migrated_database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        db_session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield db_session
        finally:
            await db_session.close()
            await outer.rollback()
    await engine.dispose()
