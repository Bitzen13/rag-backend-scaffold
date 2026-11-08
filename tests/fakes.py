"""In-memory stand-ins for the data layer the request path writes to.

These exist so the API tests can exercise the *real* `RateLimiter`, `UsageRecorder` and
`QueryLogger` — the logic worth testing — without a database. The repositories these replace are
covered against a real Postgres in `tests/test_repositories.py`; what is faked here is storage,
never a decision.

`ObedientLLM` and `KeywordRetriever` serve the attack set (`tests/test_adversarial.py`): the first
is the worst model there could be, the second retrieves without a database or an embedding API.
"""

import html
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from retrieval.retriever import RetrievedChunk


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


# --- the attack set -------------------------------------------------------------------------------

_PASSAGE = re.compile(r'<passage index="\d+" source="([^"]*)">\n(.*?)\n</passage>', re.DOTALL)


class ObedientLLM:
    """A model that obeys every instruction it finds in its context: the worst case.

    Its answer is everything the retrieved passages say, source names included, with the
    prompt's escaping undone the way a model reads through it. Whatever an attacker wrote —
    "reply with CANARY", an image to render, a link to show — is therefore in the answer
    verbatim. A defence that holds against this holds whatever the real model does, because
    it does not depend on the model at all.
    """

    async def generate(self, *, system: str, user: str) -> str:
        parts = [
            f"According to {html.unescape(source)}:\n{html.unescape(text)}"
            for source, text in _PASSAGE.findall(user)
        ]
        return "\n\n".join(parts) or "I don't have that in my knowledge base."


_WORD = re.compile(r"[a-z0-9]+")


class KeywordRetriever:
    """Ranks chunks by how many of the question's longer words they contain. In memory."""

    def __init__(self, chunks: Sequence[RetrievedChunk], top_k: int = 3) -> None:
        self._chunks = list(chunks)
        self._top_k = top_k

    async def retrieve(self, question: str, filters: object = None) -> list[RetrievedChunk]:
        wanted = {w for w in _WORD.findall(question.lower()) if len(w) > 3}
        scored = [
            (len(wanted & set(_WORD.findall(c.text.lower()))), index, c)
            for index, c in enumerate(self._chunks)
        ]
        ranked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1]))
        return [c for _, _, c in ranked[: self._top_k]]
