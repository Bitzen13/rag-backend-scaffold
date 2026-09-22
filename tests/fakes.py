"""In-memory stand-ins for the data layer the request path writes to.

These exist so the API tests can exercise the *real* `RateLimiter`, `UsageRecorder` and
`QueryLogger` — the logic worth testing — without a database. The repositories these replace are
covered against a real Postgres in `tests/test_repositories.py`; what is faked here is storage,
never a decision.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime


class FakeRateLimitStore:
    """Counts requests per (key, window) exactly as the upsert in the repository does."""

    def __init__(self) -> None:
        self.counts: dict[tuple[uuid.UUID, datetime], int] = {}
        self.purges: list[tuple[uuid.UUID, datetime]] = []

    async def count_request(self, api_key_id: uuid.UUID, window_start: datetime) -> int:
        key = (api_key_id, window_start)
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def purge_before(self, api_key_id: uuid.UUID, window_start: datetime) -> int:
        self.purges.append((api_key_id, window_start))
        stale = [k for k in self.counts if k[0] == api_key_id and k[1] < window_start]
        for k in stale:
            del self.counts[k]
        return len(stale)


@dataclass
class LoggedQuery:
    api_key_id: uuid.UUID
    question: str
    answer: str
    retrieved_chunk_ids: list[uuid.UUID]


class FakeQueryLogStore:
    """Collects what would have been written to `query_logs`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.rows: list[LoggedQuery] = []
        self.error = error

    async def record(
        self,
        *,
        api_key_id: uuid.UUID,
        question: str,
        answer: str,
        retrieved_chunk_ids: Sequence[uuid.UUID],
    ) -> object:
        if self.error is not None:
            raise self.error
        row = LoggedQuery(api_key_id, question, answer, list(retrieved_chunk_ids))
        self.rows.append(row)
        return row


@dataclass
class FakeSession:
    """A Committer that records the transaction boundaries instead of having any."""

    commits: int = 0
    rollbacks: int = 0
    commit_error: Exception | None = None

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@dataclass
class FixedClock:
    """A clock a test moves by hand, so window boundaries are exact rather than hoped for."""

    now: datetime = field(default_factory=lambda: datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = datetime.fromtimestamp(self.now.timestamp() + seconds, tz=UTC)
