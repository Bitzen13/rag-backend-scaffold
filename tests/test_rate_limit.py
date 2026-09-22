"""The rate-limit decision, over an in-memory counter and a clock a test moves by hand.

The repository's atomic upsert is covered against a real Postgres in `tests/test_repositories.py`;
what is covered here is everything the limiter decides: which window an instant belongs to, when
a request is refused, what the caller is told, and that a refused request still counts.
"""

import uuid
from datetime import UTC, datetime

from services.rate_limit import RateLimiter
from settings import RateLimitConfig
from tests.fakes import FakeRateLimitStore, FixedClock


def limiter(
    requests: int = 3, window_seconds: int = 60, clock: FixedClock | None = None
) -> tuple[RateLimiter, FakeRateLimitStore, FixedClock]:
    store = FakeRateLimitStore()
    ticking = clock or FixedClock()
    config = RateLimitConfig(requests=requests, window_seconds=window_seconds)
    return RateLimiter(store, config, clock=ticking), store, ticking


# --- windows -------------------------------------------------------------------------------------


def test_window_is_aligned_to_the_epoch_so_every_worker_agrees() -> None:
    """Two workers computing a window from the same instant must land on the same row."""
    limits, _, clock = limiter(window_seconds=60)
    clock.now = datetime(2026, 9, 21, 12, 0, 37, tzinfo=UTC)

    assert limits.window_start(clock.now) == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def test_instants_inside_one_window_share_it_and_the_next_one_does_not() -> None:
    limits, _, clock = limiter(window_seconds=60)
    clock.now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    first = limits.window_start(clock.now)

    clock.advance(59)
    assert limits.window_start(clock.now) == first
    clock.advance(1)
    assert limits.window_start(clock.now) != first


# --- counting ------------------------------------------------------------------------------------


async def test_requests_inside_the_allowance_are_allowed_and_count_down() -> None:
    limits, _, _ = limiter(requests=3)
    key = uuid.uuid4()

    decisions = [await limits.check(key) for _ in range(3)]

    assert [d.allowed for d in decisions] == [True, True, True]
    assert [d.remaining for d in decisions] == [2, 1, 0]


async def test_the_request_after_the_allowance_is_refused() -> None:
    limits, _, _ = limiter(requests=2)
    key = uuid.uuid4()
    await limits.check(key)
    await limits.check(key)

    decision = await limits.check(key)

    assert decision.allowed is False
    assert decision.remaining == 0
    assert decision.limit == 2


async def test_a_refused_request_still_counts_against_the_window() -> None:
    """Otherwise hammering a closed door would be free, and refusing is not free."""
    limits, store, clock = limiter(requests=1)
    key = uuid.uuid4()
    await limits.check(key)

    for _ in range(5):
        assert (await limits.check(key)).allowed is False

    assert store.counts[(key, limits.window_start(clock.now))] == 6


async def test_keys_have_separate_allowances() -> None:
    limits, _, _ = limiter(requests=1)
    first, second = uuid.uuid4(), uuid.uuid4()
    await limits.check(first)

    assert (await limits.check(first)).allowed is False
    assert (await limits.check(second)).allowed is True


async def test_a_new_window_restores_the_allowance() -> None:
    limits, _, clock = limiter(requests=1, window_seconds=60)
    key = uuid.uuid4()
    await limits.check(key)
    assert (await limits.check(key)).allowed is False

    clock.advance(60)

    assert (await limits.check(key)).allowed is True


async def test_opening_a_window_purges_the_key_s_finished_ones() -> None:
    """One delete per key per window keeps the table at about a row per active key."""
    limits, store, clock = limiter(requests=5, window_seconds=60)
    key = uuid.uuid4()
    await limits.check(key)
    first_window = limits.window_start(clock.now)

    clock.advance(60)
    await limits.check(key)

    assert store.purges[-1] == (key, limits.window_start(clock.now))
    assert (key, first_window) not in store.counts


async def test_purging_happens_once_per_window_not_once_per_request() -> None:
    limits, store, _ = limiter(requests=5)
    key = uuid.uuid4()

    for _ in range(5):
        await limits.check(key)

    assert len(store.purges) == 1  # only the request that opened the window


# --- what the caller is told ---------------------------------------------------------------------


async def test_retry_after_is_the_time_left_in_the_window() -> None:
    limits, _, clock = limiter(requests=1, window_seconds=60)
    clock.now = datetime(2026, 9, 21, 12, 0, 15, tzinfo=UTC)
    key = uuid.uuid4()
    await limits.check(key)

    decision = await limits.check(key)

    assert decision.retry_after_seconds == 45


async def test_retry_after_is_never_zero_so_a_caller_never_retries_instantly() -> None:
    limits, _, clock = limiter(requests=1, window_seconds=60)
    clock.now = datetime(2026, 9, 21, 12, 0, 59, 999999, tzinfo=UTC)
    key = uuid.uuid4()
    await limits.check(key)

    assert (await limits.check(key)).retry_after_seconds >= 1


async def test_allowed_decisions_carry_the_allowance_headers_but_no_retry_after() -> None:
    limits, _, _ = limiter(requests=3)

    headers = (await limits.check(uuid.uuid4())).headers()

    assert headers == {"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "2"}


async def test_a_refusal_carries_retry_after() -> None:
    limits, _, _ = limiter(requests=1)
    key = uuid.uuid4()
    await limits.check(key)

    headers = (await limits.check(key)).headers()

    assert headers["X-RateLimit-Remaining"] == "0"
    assert int(headers["Retry-After"]) >= 1


# --- turning it off ------------------------------------------------------------------------------


async def test_zero_requests_disables_limiting_without_touching_the_database() -> None:
    limits, store, _ = limiter(requests=0)

    decisions = [await limits.check(uuid.uuid4()) for _ in range(50)]

    assert limits.enabled is False
    assert all(d.allowed for d in decisions)
    assert store.counts == {}  # no row written, so no round trip spent
