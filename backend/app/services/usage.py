"""Recording that a key was used, and what it asked.

Two records with different rules:

* **`api_keys.last_used_at`** is always written, because it is what makes a key accountable — it
  is how an operator finds the key nobody uses any more and revokes it. It is written in the same
  transaction as the rate-limit count, so per-request bookkeeping costs one commit, not two.

* **`query_logs`** is written only when `api.log_queries` is on, and only after an answer exists.
  Questions and answers are user data: a deployment holding sensitive questions turns this off
  and still keeps the key accounting above. A failure to write the log is logged and swallowed,
  deliberately — the answer has already been generated and paid for, and losing it to a
  bookkeeping error would turn a minor fault into a failed request.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Protocol

from app.services.rate_limit import RateLimitDecision, RateLimiter

logger = logging.getLogger(__name__)


class KeyUsageStore(Protocol):
    """The slice of the API-key data layer this needs. Narrow on purpose: nothing here should be
    able to issue or revoke a key (see db/repositories/api_keys.py)."""

    async def touch_last_used(self, api_key_id: uuid.UUID) -> None: ...


class Committer(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UsageRecorder:
    """Per-request bookkeeping, done before the question is looked at.

    One commit covers both writes. They belong together: both are facts about the key rather than
    about the answer, both must survive a request that later fails, and committing once rather
    than twice halves what per-request accounting costs.
    """

    def __init__(self, keys: KeyUsageStore, limiter: RateLimiter, session: Committer) -> None:
        self._keys = keys
        self._limiter = limiter
        self._session = session

    async def record_request(self, api_key_id: uuid.UUID) -> RateLimitDecision:
        """Record the use, count it against the allowance, and report whether it may proceed.

        The count is committed before the caller acts on the decision, so a request that is about
        to be refused has still been counted — otherwise a caller could spend an unlimited number
        of refused requests, and refusing is not free.
        """
        await self._keys.touch_last_used(api_key_id)
        decision = await self._limiter.check(api_key_id)
        await self._session.commit()
        return decision


class QueryLogStore(Protocol):
    """The data-layer contract (see db/repositories/query_logs.py)."""

    async def record(
        self,
        *,
        api_key_id: uuid.UUID,
        question: str,
        answer: str,
        retrieved_chunk_ids: Sequence[uuid.UUID],
    ) -> object: ...


class QueryLogger:
    def __init__(self, store: QueryLogStore, session: Committer, *, enabled: bool) -> None:
        self._store = store
        self._session = session
        self._enabled = enabled

    async def record(
        self,
        api_key_id: uuid.UUID,
        question: str,
        answer: str,
        chunk_ids: Sequence[uuid.UUID],
    ) -> None:
        """Store the answered question, if this deployment logs them. Never raises."""
        if not self._enabled:
            return
        try:
            await self._store.record(
                api_key_id=api_key_id,
                question=question,
                answer=answer,
                retrieved_chunk_ids=chunk_ids,
            )
            await self._session.commit()
        except Exception:
            # The caller already has their answer; a failed log must not take it away from them.
            logger.exception("could not record the query log for api key %s", api_key_id)
            try:
                await self._session.rollback()
            except Exception:
                logger.exception("could not roll back after a failed query log write")
