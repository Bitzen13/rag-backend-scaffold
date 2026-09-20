"""Async engine/session helpers.

Repositories flush but never commit: the caller owns the transaction.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from settings import Settings

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


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(to_async_url(database_url), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
