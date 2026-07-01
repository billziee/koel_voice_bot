"""Circuit breaker + retry-with-backoff (E, PRD §12 graceful degradation).

A slow or failing upstream (Groq 429/503, NVCF cold-start, Magpie gRPC) must not
blow the per-turn latency budget or stall boot. These primitives bound retries
and stop hammering a provider that is down, so the pipeline degrades to a
fallback quickly instead of hanging.

Dependency-free and clock/rng-injectable so behaviour is deterministic in tests.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open."""


class CircuitBreaker:
    """Trips open after N consecutive failures; half-opens after a cool-off.

    States: closed (normal) → open (reject fast) → half-open (one trial). A
    success in half-open closes it; a failure re-opens the cool-off window.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        name: str = "circuit",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_s
        self._name = name
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset_timeout:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """True if a call may proceed (closed or half-open)."""
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold and self._opened_at is None:
            self._opened_at = self._clock()
            logger.warning("circuit_open name=%s failures=%d", self._name, self._failures)
        elif self.state == "half_open":
            # Trial failed — restart the cool-off.
            self._opened_at = self._clock()


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    jitter: float = 0.25,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call ``fn`` with exponential backoff + jitter, honouring a circuit breaker.

    Raises CircuitOpenError immediately if the breaker is open. Retries only on
    ``retry_on`` exceptions for which ``should_retry`` (if given) returns True.
    Re-raises the last exception after exhausting attempts.
    """
    if breaker is not None and not breaker.allow():
        raise CircuitOpenError(f"circuit '{breaker._name}' is open")

    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            result = await fn()
            if breaker is not None:
                breaker.record_success()
            return result
        except retry_on as exc:  # noqa: B902 — caller-supplied exception set
            last_exc = exc
            if breaker is not None:
                breaker.record_failure()
            retriable = should_retry(exc) if should_retry else True
            if not retriable or attempt == attempts - 1:
                raise
            # Exponential backoff, capped, with ±jitter to avoid thundering herds.
            delay = min(max_delay, base_delay * (2**attempt))
            delay *= 1 + jitter * (2 * rng() - 1)
            await sleep(max(0.0, delay))
    assert last_exc is not None  # unreachable; loop either returns or raises
    raise last_exc
