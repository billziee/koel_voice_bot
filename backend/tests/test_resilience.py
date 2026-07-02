"""Circuit breaker + retry-with-backoff (E)."""

from __future__ import annotations

import pytest

from koel.common.resilience import CircuitBreaker, CircuitOpenError, retry_async


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _noop_sleep(_: float) -> None:
    return None


def test_breaker_opens_after_threshold_and_half_opens_after_timeout():
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=2, reset_timeout_s=10, clock=clk)
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.allow()  # 1 failure, still closed
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()
    clk.t = 10  # cool-off elapsed
    assert cb.state == "half_open" and cb.allow()
    cb.record_success()
    assert cb.state == "closed"


async def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    out = await retry_async(flaky, attempts=3, sleep=_noop_sleep, rng=lambda: 0.5)
    assert out == "ok"
    assert calls["n"] == 3


async def test_retry_reraises_after_exhausting_attempts():
    async def always_fail():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await retry_async(always_fail, attempts=3, sleep=_noop_sleep)


async def test_should_retry_filter_stops_on_non_retriable():
    calls = {"n": 0}

    async def fail():
        calls["n"] += 1
        raise ValueError("400 bad request")

    with pytest.raises(ValueError):
        await retry_async(
            fail, attempts=5, sleep=_noop_sleep,
            should_retry=lambda e: "429" in str(e),  # only retry rate limits
        )
    assert calls["n"] == 1  # not retried


async def test_open_breaker_short_circuits_before_calling():
    clk = _Clock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_s=10, clock=clk)
    cb.record_failure()  # opens

    async def should_not_run():
        raise AssertionError("must not be called while open")

    with pytest.raises(CircuitOpenError):
        await retry_async(should_not_run, breaker=cb, sleep=_noop_sleep)
