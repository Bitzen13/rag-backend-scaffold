"""The engine's bounds, and the URL rewriting that gets there.

The statement timeout is the only thing standing between a query that never returns and a worker
held open for as long as it takes, so it is worth asserting that it actually reaches the driver
rather than sitting in a settings object nobody passed on.
"""

from typing import Any

import pytest

from app.db import session as db_session
from app.db.session import connect_args, create_engine, database_url_from_settings, to_async_url
from app.settings import DatabaseConfig, load_settings

URL = "postgresql://rag:rag@localhost:5432/rag"


# --- URL rewriting --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [
        "postgresql://rag:rag@localhost:5432/rag",
        "postgres://rag:rag@localhost:5432/rag",
        "postgresql+asyncpg://rag:rag@localhost:5432/rag",
    ],
)
def test_any_accepted_url_ends_up_on_the_async_driver(given: str) -> None:
    assert to_async_url(given).startswith("postgresql+asyncpg://")


def test_a_url_that_is_not_postgres_is_rejected() -> None:
    with pytest.raises(ValueError, match="postgresql://"):
        to_async_url("mysql://rag@localhost/rag")


def test_a_missing_database_url_names_itself() -> None:
    settings = load_settings(env_file=None)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        database_url_from_settings(settings)


# --- bounds ---------------------------------------------------------------------------------------


def test_the_statement_timeout_is_sent_to_postgres_in_milliseconds() -> None:
    """A server setting, so Postgres cancels the query rather than the client abandoning it."""
    args = connect_args(DatabaseConfig(statement_timeout_seconds=12.5))

    assert args["server_settings"]["statement_timeout"] == "12500"
    assert args["command_timeout"] == 12.5


def test_the_connect_timeout_is_sent_to_the_driver() -> None:
    args = connect_args(DatabaseConfig(connect_timeout_seconds=4))

    assert args["timeout"] == 4


def captured_engine_kwargs(
    monkeypatch: pytest.MonkeyPatch, database: DatabaseConfig | None
) -> dict[str, Any]:
    """What `create_engine` hands to SQLAlchemy. Asserted here rather than dug out of the engine,
    because this is the wiring under test and the engine's internals are not a contract."""
    captured: dict[str, Any] = {}

    def spy(url: str, **kwargs: Any) -> object:
        captured.update(kwargs, url=url)
        return object()

    monkeypatch.setattr(db_session, "create_async_engine", spy)
    create_engine(URL, database)
    return captured


def test_an_engine_given_bounds_passes_them_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = captured_engine_kwargs(monkeypatch, DatabaseConfig(statement_timeout_seconds=9))

    assert kwargs["connect_args"]["server_settings"]["statement_timeout"] == "9000"
    assert kwargs["url"] == "postgresql+asyncpg://rag:rag@localhost:5432/rag"


def test_an_engine_given_no_bounds_is_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI scripts pass none: cancelling an ingest transaction half way only wastes work."""
    assert "connect_args" not in captured_engine_kwargs(monkeypatch, None)


def test_stale_connections_are_discarded_rather_than_handed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pool_pre_ping` is the one database failure worth retrying transparently."""
    assert captured_engine_kwargs(monkeypatch, None)["pool_pre_ping"] is True
