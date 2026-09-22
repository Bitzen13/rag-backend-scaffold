"""Per-request bookkeeping: what is recorded, when it is committed, and what a failure costs.

The two recorders have deliberately different failure rules, and that asymmetry is the thing
worth testing. `UsageRecorder` runs before the question is answered and its failure is the
request's failure; `QueryLogger` runs after, when the answer has already been paid for, and its
failure must never take that answer away.
"""

import logging
import uuid

import pytest

from db.models import ApiKey
from services.rate_limit import RateLimiter
from services.usage import QueryLogger, UsageRecorder
from settings import RateLimitConfig
from tests.fakes import FakeQueryLogStore, FakeRateLimitStore, FakeSession
from tests.test_api_keys import FakeApiKeyStore, add_key


def recorder(
    requests: int = 5,
) -> tuple[UsageRecorder, FakeApiKeyStore, FakeSession, ApiKey]:
    keys = FakeApiKeyStore()
    plaintext = add_key(keys, label="consumer")
    row = next(k for k in keys.rows.values() if k.label == "consumer")
    assert plaintext  # the key itself is not needed here, only its row
    session = FakeSession()
    limiter = RateLimiter(FakeRateLimitStore(), RateLimitConfig(requests=requests))
    return UsageRecorder(keys, limiter, session), keys, session, row


# --- last_used_at ---------------------------------------------------------------------------------


async def test_a_request_records_when_the_key_was_last_used() -> None:
    usage, _, _, key = recorder()
    assert key.last_used_at is None

    await usage.record_request(key.id)

    assert key.last_used_at is not None


async def test_both_writes_share_one_commit() -> None:
    """Two commits per request would double what accounting costs, for no extra guarantee."""
    usage, _, session, key = recorder()

    await usage.record_request(key.id)

    assert session.commits == 1


async def test_the_count_is_committed_before_a_refusal_is_reported() -> None:
    """A refused request has still happened; it must not be free to make."""
    usage, _, session, key = recorder(requests=1)
    await usage.record_request(key.id)

    decision = await usage.record_request(key.id)

    assert decision.allowed is False
    assert session.commits == 2  # the refusing request committed its count too


async def test_the_decision_carries_the_allowance_back_to_the_caller() -> None:
    usage, _, _, key = recorder(requests=2)

    decision = await usage.record_request(key.id)

    assert (decision.limit, decision.remaining, decision.allowed) == (2, 1, True)


# --- query logging --------------------------------------------------------------------------------


async def test_an_answered_question_is_recorded_with_its_chunks() -> None:
    store = FakeQueryLogStore()
    session = FakeSession()
    key_id, chunk_ids = uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()]

    await QueryLogger(store, session, enabled=True).record(
        key_id, "Why is the sky blue?", "Rayleigh scattering.", chunk_ids
    )

    assert len(store.rows) == 1
    row = store.rows[0]
    assert (row.api_key_id, row.question, row.answer) == (
        key_id,
        "Why is the sky blue?",
        "Rayleigh scattering.",
    )
    assert row.retrieved_chunk_ids == chunk_ids
    assert session.commits == 1


async def test_logging_off_writes_nothing_and_commits_nothing() -> None:
    """A deployment holding sensitive questions turns this off and keeps the key accounting."""
    store = FakeQueryLogStore()
    session = FakeSession()

    await QueryLogger(store, session, enabled=False).record(
        uuid.uuid4(), "something private", "an answer", []
    )

    assert store.rows == []
    assert session.commits == 0


async def test_an_empty_retrieval_is_still_recorded() -> None:
    store = FakeQueryLogStore()

    await QueryLogger(store, FakeSession(), enabled=True).record(
        uuid.uuid4(), "off topic", "not in my knowledge base", []
    )

    assert store.rows[0].retrieved_chunk_ids == []


@pytest.mark.parametrize(
    "failure",
    [
        {"store_error": RuntimeError("the log table is gone")},
        {"commit_error": RuntimeError("connection reset")},
    ],
)
async def test_a_failed_log_never_raises_at_the_caller(
    failure: dict[str, Exception], caplog: pytest.LogCaptureFixture
) -> None:
    """The answer exists and has been paid for; a bookkeeping fault must not lose it."""
    store = FakeQueryLogStore(failure.get("store_error"))
    session = FakeSession(commit_error=failure.get("commit_error"))

    with caplog.at_level(logging.ERROR):
        await QueryLogger(store, session, enabled=True).record(
            uuid.uuid4(), "a question", "an answer", []
        )

    assert "could not record the query log" in caplog.text


async def test_a_failed_log_rolls_back_so_the_session_is_usable_afterwards() -> None:
    session = FakeSession()

    await QueryLogger(FakeQueryLogStore(RuntimeError("boom")), session, enabled=True).record(
        uuid.uuid4(), "a question", "an answer", []
    )

    assert session.rollbacks == 1
