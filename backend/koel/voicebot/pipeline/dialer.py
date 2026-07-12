"""Outbound retry policy (1.2) — the pure, deterministic scheduling core.

A campaign carries a ``retry_policy`` JSON blob; this module turns a dial's
*outcome* + how many times we've already tried into "when do we try again, if at
all". Kept free of I/O so it's exhaustively unit-testable without a DB, a clock,
or a carrier — the persistence half (claim/enqueue/record) lives in
``koel.voicebot.campaigns`` and the origination half is dormant until the
telephony bridge (1.1) is wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Outcomes that mean "the customer was reached" — never retry, the call is done.
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {"answered", "completed", "promise_to_pay", "opted_out", "dnd"}
)
# Outcomes worth another attempt (subject to max_attempts). Anything not in
# either set is treated as non-retryable (fail closed — don't hammer a number on
# an outcome we don't recognise).
DEFAULT_RETRY_OUTCOMES: frozenset[str] = frozenset(
    {"no_answer", "busy", "voicemail", "failed", "network_error"}
)

# Cap the exponential so a long-running campaign can't schedule a retry weeks out.
_MAX_BACKOFF_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RetryPolicy:
    """How an outbound campaign retries an unconnected dial. Immutable; build one
    per campaign from its JSON via :func:`policy_from_config`."""

    max_attempts: int = 3
    backoff_seconds: int = 900  # 15 min before the 2nd attempt
    backoff_factor: float = 2.0  # then exponential: 15m, 30m, 60m, …
    retry_outcomes: frozenset[str] = field(default_factory=lambda: DEFAULT_RETRY_OUTCOMES)


DEFAULT_RETRY_POLICY = RetryPolicy()


def policy_from_config(config: dict | None) -> RetryPolicy:
    """Parse a campaign's ``retry_policy`` blob into a :class:`RetryPolicy`,
    clamping to safe bounds. Unknown/missing keys fall back to the defaults, so a
    ``{}`` (or ``None``) policy is valid and behaves sensibly."""
    if not config:
        return DEFAULT_RETRY_POLICY
    max_attempts = max(1, int(config.get("max_attempts", DEFAULT_RETRY_POLICY.max_attempts)))
    backoff_seconds = max(
        1, int(config.get("backoff_seconds", DEFAULT_RETRY_POLICY.backoff_seconds))
    )
    backoff_factor = max(
        1.0, float(config.get("backoff_factor", DEFAULT_RETRY_POLICY.backoff_factor))
    )
    outcomes = config.get("retry_outcomes")
    retry_outcomes = (
        frozenset(str(o) for o in outcomes) if outcomes else DEFAULT_RETRY_OUTCOMES
    )
    return RetryPolicy(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        backoff_factor=backoff_factor,
        retry_outcomes=retry_outcomes,
    )


def should_retry(policy: RetryPolicy, attempt_count: int, outcome: str) -> bool:
    """True iff another attempt is warranted: the outcome is retryable (and not
    terminal) and we haven't exhausted ``max_attempts``. ``attempt_count`` is the
    number of attempts *already made* (the just-finished dial included)."""
    if outcome in TERMINAL_OUTCOMES:
        return False
    if outcome not in policy.retry_outcomes:
        return False
    return attempt_count < policy.max_attempts


def next_retry_delay(policy: RetryPolicy, attempt_count: int) -> timedelta:
    """Exponential backoff for the next attempt, capped. ``attempt_count`` is the
    number already made, so the 1st retry (after attempt #1) uses the base delay."""
    exponent = max(0, attempt_count - 1)
    seconds = policy.backoff_seconds * (policy.backoff_factor**exponent)
    return timedelta(seconds=min(seconds, _MAX_BACKOFF_SECONDS))


def next_retry_at(
    policy: RetryPolicy, attempt_count: int, outcome: str, now: datetime
) -> datetime | None:
    """When to schedule the next attempt, or ``None`` to give up (terminal outcome
    or retries exhausted). Pure: the caller supplies ``now`` so the result is
    reproducible in tests."""
    if not should_retry(policy, attempt_count, outcome):
        return None
    return now + next_retry_delay(policy, attempt_count)
