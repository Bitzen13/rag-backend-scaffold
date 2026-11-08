"""Structured logging, and the claim Phase 8 has to be able to make: no secret reaches a log.

The redaction is tested where it actually runs — in the formatter — because that is the point of
putting it there. A test that only called `redact()` would pass while a log line written by a
library, or an exception traceback, went out in the clear.
"""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app import observability
from app.observability import (
    REDACTED,
    JsonFormatter,
    TextFormatter,
    build_formatter,
    configure_logging,
    install_handler,
    redact,
    register_secrets,
    secret_values,
)
from app.settings import load_settings

DATABASE_URL = "postgresql://rag:s3cret-password@db.internal:5432/rag"
ISSUED_KEY = "rag_zK3xQ8vN2mP7wL9tR4yB6cD1fH5jS0aG"
PROVIDER_KEY = "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZ"


@pytest.fixture
def captured() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger wired to a formatter under test, isolated from the root logger."""
    stream = io.StringIO()
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, stream
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def attach(logger: logging.Logger, stream: io.StringIO, formatter: logging.Formatter) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# --- redaction ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        ISSUED_KEY,
        PROVIDER_KEY,
        "sk-proj-abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnop",
        "api-key=abcdefghijklmnop",
    ],
)
def test_things_that_look_like_secrets_are_redacted(value: str) -> None:
    result = redact(f"call failed with {value}")

    assert REDACTED in result
    assert value not in result


def test_a_url_keeps_its_host_and_user_but_loses_its_password() -> None:
    result = redact(f"connecting to {DATABASE_URL}")

    assert "s3cret-password" not in result
    assert "db.internal:5432/rag" in result  # still diagnosable
    assert "rag:" in result  # the user name is not a secret


def test_a_registered_secret_is_redacted_even_when_it_looks_like_nothing() -> None:
    """A database password that happens to be an ordinary word matches no pattern."""
    register_secrets(["correcthorse"])

    assert "correcthorse" not in redact("the password is correcthorse")


def test_short_values_are_not_registered() -> None:
    """Blanking every occurrence of a three-character string would destroy the log."""
    register_secrets(["abc"])

    assert redact("abc appears in abcdef") == "abc appears in abcdef"


@pytest.mark.parametrize(
    "line",
    [
        "chunked report.pdf: 512 chunks, tokens min/avg/max = 40/380/500",
        "opening risk-management-policy.pdf: hash changed",  # `sk-` inside a word
        "desk-booking-guide.md: 12 chunks",
        "token count 512 exceeds the chunk size",  # `token` as an ordinary word
        "the token is required; the api key header is missing",
    ],
)
def test_ordinary_text_survives_untouched(line: str) -> None:
    assert redact(line) == line


@pytest.mark.parametrize(
    "value", ["token=abcdefghijklmnop", "token: abcdefghijklmnop", "API_KEY = abcdefghijklmnop"]
)
def test_a_token_or_key_assignment_is_still_redacted(value: str) -> None:
    assert "abcdefghijklmnop" not in redact(f"config {value}")


# --- redaction happens in the formatter, so nothing can route around it ---------------------------


@pytest.mark.parametrize("kind", ["text", "json"])
def test_a_secret_passed_as_a_log_argument_never_reaches_the_stream(
    captured: tuple[logging.Logger, io.StringIO], kind: Any
) -> None:
    logger, stream = captured
    attach(logger, stream, build_formatter(kind))

    logger.info("authenticating with %s against %s", ISSUED_KEY, DATABASE_URL)

    written = stream.getvalue()
    assert ISSUED_KEY not in written
    assert "s3cret-password" not in written
    assert REDACTED in written


@pytest.mark.parametrize("kind", ["text", "json"])
def test_a_secret_inside_an_exception_traceback_is_redacted_too(
    captured: tuple[logging.Logger, io.StringIO], kind: Any
) -> None:
    logger, stream = captured
    attach(logger, stream, build_formatter(kind))

    try:
        raise RuntimeError(f"could not connect to {DATABASE_URL}")
    except RuntimeError:
        logger.exception("upstream failure")

    written = stream.getvalue()
    assert "s3cret-password" not in written
    assert "upstream failure" in written


# --- structure ------------------------------------------------------------------------------------


def test_json_lines_are_one_object_each_with_the_extras_as_fields(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured
    attach(logger, stream, JsonFormatter())

    logger.info("request", extra={"status": 200, "duration_ms": 12.5, "path": "/api/query"})

    record = json.loads(stream.getvalue())
    assert record["message"] == "request"
    assert record["level"] == "INFO"
    assert record["status"] == 200
    assert record["duration_ms"] == 12.5
    assert record["path"] == "/api/query"
    assert "time" in record


def test_json_never_emits_the_internal_logrecord_attributes(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured
    attach(logger, stream, JsonFormatter())

    logger.warning("something")

    record = json.loads(stream.getvalue())
    assert not {"msg", "args", "levelno", "pathname", "lineno"} & set(record)


def test_text_lines_show_the_extras_too(captured: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = captured
    attach(logger, stream, TextFormatter())

    logger.info("request", extra={"status": 429})

    assert "request [status=429]" in stream.getvalue()


def test_text_lines_without_extras_have_no_empty_bracket(
    captured: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = captured
    attach(logger, stream, TextFormatter())

    logger.info("plain")

    assert stream.getvalue().strip().endswith("plain")


# --- configuration --------------------------------------------------------------------------------


def test_configure_logging_registers_this_deployment_s_own_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    settings = load_settings()

    values = secret_values(settings)

    assert DATABASE_URL in values
    assert PROVIDER_KEY in values
    assert "s3cret-password" in values  # the password alone, since URLs are logged whole


def test_install_handler_replaces_its_own_handler_rather_than_stacking() -> None:
    """Calling it twice must not double every line."""
    root = logging.getLogger()
    before = list(root.handlers)
    # One installed by an earlier test (or script import) is replaced too, not added to.
    others = [h for h in before if h is not observability._handler]
    try:
        install_handler(level=logging.INFO, stream=io.StringIO())
        install_handler(level=logging.INFO, stream=io.StringIO())

        assert len(root.handlers) == len(others) + 1
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


def test_install_handler_leaves_handlers_it_did_not_install_alone() -> None:
    """A capture something else attached deliberately must not be silently switched off."""
    root = logging.getLogger()
    other = logging.StreamHandler(io.StringIO())
    root.addHandler(other)
    before = list(root.handlers)
    try:
        install_handler(level=logging.INFO, stream=io.StringIO())

        assert other in root.handlers
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
        root.removeHandler(other)


def test_configure_logging_applies_the_configured_format_and_level() -> None:
    root = logging.getLogger()
    before, before_level = list(root.handlers), root.level
    stream = io.StringIO()
    try:
        base = load_settings(env_file=None)
        settings = base.model_copy(
            update={
                "logging": base.logging.model_copy(update={"format": "json", "level": "WARNING"})
            }
        )
        configure_logging(settings, stream=stream)

        installed = [h for h in root.handlers if h not in before]
        assert root.level == logging.WARNING
        assert len(installed) == 1
        assert isinstance(installed[0].formatter, JsonFormatter)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
        root.setLevel(before_level)
