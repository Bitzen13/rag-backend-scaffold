"""Async engine/session helpers.

Repositories flush but never commit: the caller owns the transaction.

Every database call is bounded by a statement timeout, so a query that will never return is
cancelled by Postgres instead of holding a request — and a worker — open. The vector search and
the typo-tolerant keyword search are Postgres queries too, so one timeout covers all of them.
Statements are not retried inside a request: the failure is surfaced as a 503, because a blind
retry of a statement that may have been a write is worse than the failure it hides.
"""

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import DatabaseConfig, Settings

_ASYNC_DRIVER = "postgresql+asyncpg://"


def to_async_url(database_url: str) -> str:
    """Rewrite a plain postgresql:// URL to use the asyncpg driver."""
    if database_url.startswith(_ASYNC_DRIVER):
        return database_url
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return _ASYNC_DRIVER + database_url.removeprefix(prefix)
    raise ValueError("DATABASE_URL must start with postgresql:// or postgres://")


def database_url_from_settings(settings: Settings) -> str:
    url = settings.secrets.database_url
    if url is None:
        raise RuntimeError("DATABASE_URL is not set")
    return url.get_secret_value()


def connect_args(database: DatabaseConfig) -> dict[str, Any]:
    """asyncpg connection arguments carrying the configured bounds.

    `statement_timeout` is a server setting, so Postgres itself cancels the query rather than the
    client walking away from one that keeps running. `command_timeout` bounds the client side as
    well, for the case where the connection stops answering altogether.
    """
    milliseconds = int(database.statement_timeout_seconds * 1000)
    return {
        "timeout": database.connect_timeout_seconds,
        "command_timeout": database.statement_timeout_seconds,
        "server_settings": {"statement_timeout": str(milliseconds)},
    }


def create_engine(database_url: str, database: DatabaseConfig | None = None) -> AsyncEngine:
    """The engine a process shares. `pool_pre_ping` discards a connection the server has already
    closed, which is the one database failure worth retrying transparently.

    `database` bounds every statement, and the API passes it. The CLI scripts deliberately do
    not: ingestion writes a whole document in one transaction, and cancelling that half way
    wastes the embedding bill already paid, where a request that hangs costs a worker.
    """
    bounds = {} if database is None else {"connect_args": connect_args(database)}
    return create_async_engine(to_async_url(database_url), pool_pre_ping=True, **bounds)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
