"""Campaign dialer (1.2): pure retry policy + the enqueue/claim/record queue.

The origination half (actually placing the call) is dormant until the telephony
bridge (1.1); everything schedulable/verifiable without a carrier lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from koel.voicebot.campaigns import (
    claim_due_attempts,
    enqueue_dial,
    record_dial_result,
)
from koel.voicebot.pipeline.dialer import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    next_retry_at,
    next_retry_delay,
    policy_from_config,
    should_retry,
)

_NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)


# --- pure policy ------------------------------------------------------------


def test_terminal_outcome_never_retries():
    assert should_retry(DEFAULT_RETRY_POLICY, attempt_count=1, outcome="answered") is False
    assert next_retry_at(DEFAULT_RETRY_POLICY, 1, "promise_to_pay", _NOW) is None


def test_unknown_outcome_fails_closed():
    # An outcome in neither the retry nor terminal set is not retried.
    assert should_retry(DEFAULT_RETRY_POLICY, 1, "who_knows") is False


def test_retryable_outcome_reschedules_until_max_attempts():
    policy = RetryPolicy(max_attempts=3, backoff_seconds=900, backoff_factor=2.0)
    assert next_retry_at(policy, attempt_count=1, outcome="no_answer", now=_NOW) == _NOW + timedelta(
        seconds=900
    )
    assert next_retry_at(policy, attempt_count=2, outcome="busy", now=_NOW) == _NOW + timedelta(
        seconds=1800
    )
    # Third attempt already made == max_attempts → give up.
    assert next_retry_at(policy, attempt_count=3, outcome="no_answer", now=_NOW) is None


def test_backoff_is_capped():
    policy = RetryPolicy(max_attempts=100, backoff_seconds=3600, backoff_factor=10.0)
    # 3600 * 10^5 would be weeks; must clamp to the 24h ceiling.
    assert next_retry_delay(policy, attempt_count=6) == timedelta(hours=24)


def test_policy_from_config_clamps_and_defaults():
    assert policy_from_config(None) is DEFAULT_RETRY_POLICY
    assert policy_from_config({}) is DEFAULT_RETRY_POLICY
    parsed = policy_from_config(
        {"max_attempts": 0, "backoff_seconds": -5, "backoff_factor": 0.1, "retry_outcomes": ["x"]}
    )
    assert parsed.max_attempts == 1  # clamped up from 0
    assert parsed.backoff_seconds == 1  # clamped up from -5
    assert parsed.backoff_factor == 1.0  # clamped up from 0.1
    assert parsed.retry_outcomes == frozenset({"x"})


# --- persistence queue ------------------------------------------------------


async def test_enqueue_then_claim_marks_in_progress(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+911234567890"
    )
    claimed = await claim_due_attempts(limit=10)
    assert len(claimed) == 1
    assert claimed[0]["id"] == attempt_id
    assert claimed[0]["status"] == "in_progress"
    assert claimed[0]["attempt_count"] == 1


async def test_future_scheduled_dial_is_not_claimed_yet(test_db):
    await enqueue_dial(
        campaign_id=None,
        org_id="org1",
        customer_id="c1",
        phone="+911111111111",
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert await claim_due_attempts(limit=10) == []


async def test_record_no_answer_reschedules_pending(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+912222222222"
    )
    await claim_due_attempts(limit=10)
    updated = await record_dial_result(attempt_id, "no_answer")
    assert updated is not None
    assert updated["status"] == "pending"  # queued for a retry
    assert updated["last_outcome"] == "no_answer"
    # ...and it's scheduled in the future, so it won't be re-claimed immediately.
    assert await claim_due_attempts(limit=10) == []


async def test_record_answered_marks_done(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+913333333333"
    )
    await claim_due_attempts(limit=10)
    updated = await record_dial_result(attempt_id, "answered")
    assert updated is not None
    assert updated["status"] == "done"


async def _force_due(attempt_id: str) -> None:
    """Rewind a rescheduled attempt so the next claim picks it up — stands in for
    wall-clock time passing between retries."""
    from koel.db.models import DialAttempt
    from koel.db.session import session_scope

    async with session_scope() as db:
        row = await db.get(DialAttempt, attempt_id)
        row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_retries_exhaust_to_failed(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+914444444444"
    )
    # Default policy = 3 attempts. Claim+fail three times: the first two reschedule
    # (pending), the third exhausts the budget and closes the row (failed).
    statuses = []
    for i in range(3):
        claimed = await claim_due_attempts(limit=10)
        assert len(claimed) == 1, f"attempt {i} should be due and claimable"
        result = await record_dial_result(attempt_id, "no_answer")
        assert result is not None
        statuses.append(result["status"])
        await _force_due(attempt_id)

    assert statuses == ["pending", "pending", "failed"]
    # The row is terminal; a further record on a non-in_progress row is a no-op.
    assert await record_dial_result(attempt_id, "no_answer") is None


async def test_record_on_unknown_id_returns_none(test_db):
    assert await record_dial_result("does-not-exist", "no_answer") is None
