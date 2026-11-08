"""Structured logging, and the guarantee that a secret never reaches a log.

Two jobs, deliberately in one module because the second one only works if it cannot be bypassed:

1. **Formatting.** `text` for a terminal, `json` for production — one object per line, so a log
   shipper indexes fields instead of parsing prose. Anything passed to a log call as an `extra`
   becomes a field, which is what makes a request line queryable.

2. **Redaction.** Both formatters redact on the way out, in `format()`, rather than at the call
   site. A filter or a careful caller can be forgotten; a formatter cannot be, because every
   record — including ones from libraries this project does not own, and including exception
   tracebacks — passes through it. `redact()` covers the shapes a secret takes here (an issued
   API key, a provider key, a password inside a database URL, a bearer token) and, once
   `configure_logging` has seen the settings, the exact secret values this deployment holds.
"""

import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator
from typing import Any, Literal

from app.settings import Settings

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 8  # shorter than this is not a secret, and blanking it would be noise

# Shapes a secret takes, whoever logged it. `rag_` is this project's own API key prefix, which is
# exactly why keys carry it: a leaked one is recognisable in a paste, and matchable here.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rag_[A-Za-z0-9_\-]{8,}"),
    # OpenAI and Anthropic keys. The left boundary stops `risk-management-policy.pdf` matching.
    re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    # `token` and `api key` only as an assignment (`token=...`, `api-key: ...`), so ordinary prose
    # such as "token count 512" or "the token is required" is left alone.
    re.compile(r"(?i)\b(?:token|api[-_ ]?key)\b\s*[=:]\s*\S+"),
    # A password inside a connection URL: postgresql://user:password@host/db
    re.compile(r"(?<=://)([^:/\s@]+):([^@/\s]+)(?=@)"),
)

# Exact values this deployment holds, registered by `configure_logging`. A secret that matches no
# pattern above (a database password that happens to look like a word) is still caught here.
_known_secrets: set[str] = set()

# The handler this module last installed, so a second call replaces it instead of stacking.
_handler: logging.Handler | None = None


def register_secrets(values: Iterable[str]) -> None:
    """Add exact values to redact. Idempotent; short values are ignored as unmatchable noise."""
    _known_secrets.update(v for v in values if v and len(v) >= MIN_SECRET_LENGTH)


def secret_values(settings: Settings) -> list[str]:
    """Every secret this deployment was given, as plaintext, for redaction only."""
    secrets = settings.secrets
    held = (
        secrets.anthropic_api_key,
        secrets.openai_api_key,
        secrets.database_url,
        secrets.vector_db_url,
        secrets.admin_api_token,
    )
    values = [s.get_secret_value() for s in held if s is not None]
    # A database URL is logged whole often enough that its password is worth registering alone.
    return values + [password for url in values for password in _url_passwords(url)]


def _url_passwords(value: str) -> Iterator[str]:
    for match in re.finditer(r"://[^:/\s@]+:([^@/\s]+)@", value):
        yield match.group(1)


def redact(text: str) -> str:
    """Replace anything that looks like, or is known to be, a secret."""
    for secret in _known_secrets:
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        # The URL pattern keeps the user name and blanks only the password.
        replacement = rf"\1:{REDACTED}" if pattern.groups == 2 else REDACTED
        text = pattern.sub(replacement, text)
    return text


DEFAULT_TEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"


class TextFormatter(logging.Formatter):
    """Human-readable, with any `extra` fields appended so a terminal shows them too."""

    def __init__(self, fmt: str = DEFAULT_TEXT_FORMAT) -> None:
        super().__init__(fmt=fmt)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = " ".join(f"{key}={value}" for key, value in sorted(extra_fields(record).items()))
        return redact(f"{line} [{fields}]" if fields else line)


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `extra` fields become top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **extra_fields(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


# Attributes every LogRecord carries; anything else on one was passed as `extra`.
_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | frozenset(
    {"message", "asctime", "taskName"}
)


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_FIELDS}


def build_formatter(
    kind: Literal["text", "json"], text_format: str = DEFAULT_TEXT_FORMAT
) -> logging.Formatter:
    return JsonFormatter() if kind == "json" else TextFormatter(text_format)


def install_handler(
    *,
    level: int | str = logging.INFO,
    kind: Literal["text", "json"] = "text",
    text_format: str = DEFAULT_TEXT_FORMAT,
    stream: Any = None,
) -> None:
    """Put a redacting handler on the root logger, replacing whatever was there.

    Replacing rather than adding means calling it twice — a reload, a test, a script that
    bootstraps logging before its settings are loaded — does not duplicate every line. Only the
    handler this function installed is replaced; anything else on the root logger stays.

    This is also what a CLI calls before it has a `Settings` to hand: pattern redaction works
    without one, and `configure_logging` adds the deployment's exact secrets as soon as there is.
    """
    global _handler
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(build_formatter(kind, text_format))
    root = logging.getLogger()
    if _handler is not None:
        # Replace our own previous handler, and only that one. Handlers something else attached
        # on purpose — a test's capture, a sidecar — are left alone: removing every handler here
        # would silently disable them, which is a surprising thing for a logging setup to do.
        root.removeHandler(_handler)
    root.addHandler(handler)
    _handler = handler
    root.setLevel(level)
    # One line per outbound provider request is enough; their DEBUG output includes headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure_logging(settings: Settings, *, stream: Any = None) -> None:
    """Install the configured formatter and register this deployment's secrets for redaction."""
    register_secrets(secret_values(settings))
    install_handler(level=settings.logging.level, kind=settings.logging.format, stream=stream)
