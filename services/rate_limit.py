"""Per-key rate limiting: how many requests a key may make, and what to tell it when it may not.

The decision is a fixed window. `window_start` is the beginning of the window the current instant
falls in, so every worker serving the same key agrees on which window it is without any of them
having to coordinate — the shared state is one row, and the count is a single atomic statement.

The trade a fixed window makes is a boundary burst: a caller can spend its whole allowance at the
end of one window and again at the start of the next, so twice the limit in a short span. A
sliding window would close that at the cost of a stored row per request. For protecting an LLM
bill from a runaway consumer, the burst is affordable and the row per request is not.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from settings import RateLimitConfig


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome for one request, and the numbers a caller needs to behave well."""

    allowed: bool
    limit: int  # requests permitted per window
    remaining: int  # still available in this window, never below zero
    retry_after_seconds: int  # whole seconds until this window ends

    def headers(self) -> dict[str, str]:
        """Response headers, in the widely-understood `X-RateLimit-*` shape."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class RateLimitStore(Protocol):
    """The data-layer contract (see db/repositories/rate_limits.py)."""

    async def count_request(self, api_key_id: uuid.UUID, window_start: datetime) -> int: ...

    async def purge_before(self, api_key_id: uuid.UUID, window_start: datetime) -> int: ...


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RateLimiter:
    def __init__(
        self, store: RateLimitStore, config: RateLimitConfig, *, clock: Clock = _utc_now
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._config.requests > 0

    def window_start(self, now: datetime) -> datetime:
        """The start of the window `now` falls in, aligned to the epoch so every worker agrees."""
        window = self._config.window_seconds
        seconds = int(now.timestamp()) // window * window
        return datetime.fromtimestamp(seconds, tz=UTC)

    async def check(self, api_key_id: uuid.UUID) -> RateLimitDecision:
        """Count this request against the key's allowance and say whether it may proceed.

        The request is counted whether or not it is allowed: a caller that keeps hammering a
        closed door does not get its window reset for trying.
        """
        limit = self._config.requests
        if not self.enabled:  # limiting off: no row written, no round trip spent
            return RateLimitDecision(True, limit, limit, 0)

        now = self._clock()
        start = self.window_start(now)
        used = await self._store.count_request(api_key_id, start)
        if used == 1:
            # A window just opened for this key, so its earlier ones are finished and can go.
            # Doing it here rather than on every request costs one delete per key per window.
            await self._store.purge_before(api_key_id, start)
        ends_at = start.timestamp() + self._config.window_seconds
        return RateLimitDecision(
            allowed=used <= limit,
            limit=limit,
            remaining=max(limit - used, 0),
            retry_after_seconds=max(int(ends_at - now.timestamp()), 1),
        )
