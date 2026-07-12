"""Campaign dial-queue persistence (1.2) — enqueue, claim, record.

The dialer worker (dormant until the telephony bridge, 1.1, can originate) drives
this queue: it claims a batch of *due* ``DialAttempt`` rows, dials each, then
records the outcome — which either reschedules a retry or closes the row per the
campaign's :class:`~koel.voicebot.pipeline.dialer.RetryPolicy`.

Concurrency: claiming uses ``FOR UPDATE SKIP LOCKED`` on Postgres so multiple
workers never grab the same row (no double-dialing). SQLite (tests) has no such
lock, so the claim degrades to a plain windowed select+update inside the single
transaction — correct for the single-worker dev/test case, and the code path is
selected by dialect at runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from koel.common.logging import get_logger
from koel.db.models import Campaign, DialAttempt, DialAttemptStatus
from koel.db.session import session_scope
from koel.voicebot.pipeline.dialer import (
    TERMINAL_OUTCOMES,
    RetryPolicy,
    next_retry_at,
    policy_from_config,
)

logger = get_logger("koel.voicebot.campaigns")

# Hard ceiling on a single claim batch so a worker can't drain the whole queue
# into one transaction and starve its peers.
_MAX_CLAIM_LIMIT = 100


def _now() -> datetime:
    return datetime.now(UTC)


def _attempt_to_dict(row: DialAttempt) -> dict:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "campaign_id": row.campaign_id,
        "customer_id": row.customer_id,
        "phone": row.phone,
        "status": row.status.value,
        "attempt_count": row.attempt_count,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "last_outcome": row.last_outcome,
    }


async def enqueue_dial(
    *,
    campaign_id: str | None,
    org_id: str | None,
    customer_id: str | None,
    phone: str,
    scheduled_at: datetime | None = None,
) -> str:
    """Queue one outbound dial. ``scheduled_at`` defaults to now (dial ASAP); pass
    a future time for a delayed/first-attempt-later enqueue. Returns the row id."""
    async with session_scope() as db:
        row = DialAttempt(
            campaign_id=campaign_id,
            org_id=org_id,
            customer_id=customer_id,
            phone=phone,
            status=DialAttemptStatus.pending,
            scheduled_at=scheduled_at or _now(),
        )
        db.add(row)
        await db.flush()
        return row.id


async def claim_due_attempts(limit: int = 10, now: datetime | None = None) -> list[dict]:
    """Atomically claim up to ``limit`` due dials (``pending`` + ``scheduled_at`` <=
    now), mark them ``in_progress``, bump ``attempt_count``, and return them. On
    Postgres this uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never
    claim the same row; on SQLite it's a plain select+update in one transaction."""
    now = now or _now()
    limit = max(1, min(limit, _MAX_CLAIM_LIMIT))
    async with session_scope() as db:
        stmt = (
            select(DialAttempt)
            .where(
                DialAttempt.status == DialAttemptStatus.pending,
                DialAttempt.scheduled_at <= now,
            )
            .order_by(DialAttempt.scheduled_at.asc())
            .limit(limit)
        )
        # SKIP LOCKED is Postgres-only; SQLite's dialect rejects the row lock.
        if _supports_skip_locked(db):
            stmt = stmt.with_for_update(skip_locked=True)
        rows = list((await db.execute(stmt)).scalars().all())
        for row in rows:
            row.status = DialAttemptStatus.in_progress
            row.attempt_count += 1
        await db.flush()
        return [_attempt_to_dict(r) for r in rows]


async def record_dial_result(attempt_id: str, outcome: str) -> dict | None:
    """Apply a dial's outcome: reschedule a retry (back to ``pending`` at the
    policy's next time) or close the row (``done`` on a terminal outcome / reached
    customer, ``failed`` once retries are exhausted). Returns the updated row, or
    ``None`` if it's gone or wasn't in flight."""
    async with session_scope() as db:
        row = await db.get(DialAttempt, attempt_id)
        if row is None or row.status != DialAttemptStatus.in_progress:
            return None
        row.last_outcome = outcome
        policy = await _policy_for(db, row.campaign_id)
        retry_at = next_retry_at(policy, row.attempt_count, outcome, _now())
        if retry_at is not None:
            row.status = DialAttemptStatus.pending
            row.scheduled_at = retry_at
        elif _is_reached(outcome):
            # Reached the customer — done, regardless of attempt count.
            row.status = DialAttemptStatus.done
        else:
            # Non-retryable outcome or retries exhausted — give up.
            row.status = DialAttemptStatus.failed
        await db.flush()
        return _attempt_to_dict(row)


async def _policy_for(db: AsyncSession, campaign_id: str | None) -> RetryPolicy:
    if campaign_id is None:
        return policy_from_config(None)
    campaign = await db.get(Campaign, campaign_id)
    return policy_from_config(campaign.retry_policy if campaign else None)


def _is_reached(outcome: str) -> bool:
    return outcome in TERMINAL_OUTCOMES


def _supports_skip_locked(db: AsyncSession) -> bool:
    """Row-level ``SKIP LOCKED`` exists on Postgres/MySQL, not SQLite."""
    return db.bind is not None and db.bind.dialect.name in {"postgresql", "mysql"}
