"""Per-IP rate limiting (M, PRD §08 abuse controls).

Two interchangeable backends behind one FastAPI dependency (O-8):

- ``RateLimiter`` — dependency-free in-process fixed window. Correct for a single
  worker (dev / tests / the pilot), but its counters live in one process, so with
  ``uvicorn --workers >1`` or a second instance the effective limit multiplies by
  the process count and resets on deploy.
- ``RedisRateLimiter`` — the same fixed window shared across all workers and
  instances via an atomic Redis ``INCR`` + ``EXPIRE NX``. Selected by
  ``CommonSettings.rate_limit_backend == "redis"``.

The ``rate_limit()`` dependency signature and call sites are unchanged; the
backend is chosen from settings at first use, with in-memory as the fallback.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from inspect import isawaitable

from fastapi import Request, status

from koel.common.config import CommonSettings
from koel.common.errors import ApiError
from koel.common.logging import get_logger

logger = get_logger("koel.ratelimit")


class RateLimiter:
    """Fixed-cap sliding window keyed by an arbitrary string (usually client IP)."""

    def __init__(
        self,
        *,
        max_requests: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._window = window_s
        self._clock = clock
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._checks = 0

    # Sweep idle keys every N checks so the dict can't grow unbounded with unique
    # IPs (a cheap defence against memory exhaustion from spoofed source IPs).
    _SWEEP_EVERY = 1024

    def _maybe_sweep(self, now: float) -> None:
        self._checks += 1
        if self._checks % self._SWEEP_EVERY:
            return
        for k in [k for k, ts in self._hits.items() if not ts or now - ts[-1] >= self._window]:
            del self._hits[k]

    def check(self, key: str) -> bool:
        """Record a hit; return True if within the limit, False if it should be rejected."""
        now = self._clock()
        self._maybe_sweep(now)
        bucket = [t for t in self._hits[key] if now - t < self._window]
        if len(bucket) >= self._max:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True


class RedisRateLimiter:
    """Fixed-window limiter shared across workers/instances via Redis.

    Each key maps to a Redis counter with a ``window_s`` TTL. A hit does an atomic
    ``INCR`` + ``EXPIRE key window NX`` in a transactional pipeline: the first hit
    creates the counter and stamps the TTL; later hits within the window only
    increment (``NX`` leaves the original TTL, so the window is a true fixed window
    that resets when the key expires, not a rolling one). Atomicity via MULTI/EXEC
    means there is no window where the counter exists without a TTL, so a mid-op
    crash can't orphan a key and permanently block an IP.

    Fails **open**: if Redis is unreachable the hit is allowed (a rate limiter is
    defence-in-depth on already-authenticated endpoints, so an infra blip must not
    turn into a login outage). The event is logged.
    """

    def __init__(self, *, max_requests: int, window_s: float, client, namespace: str = "koel:rl:") -> None:  # noqa: ANN001 — redis client type varies
        self._max = max_requests
        self._window = max(1, int(window_s))
        self._redis = client
        self._ns = namespace

    async def check(self, key: str) -> bool:
        rk = f"{self._ns}{key}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(rk)
                pipe.expire(rk, self._window, nx=True)
                count, _ = await pipe.execute()
        except Exception:  # noqa: BLE001 — Redis unreachable: fail open, never block auth on infra
            logger.warning("rate limiter: redis unavailable, allowing request (fail-open)")
            return True
        return int(count) <= self._max


def _build_limiter(max_requests: int, window_s: float):  # noqa: ANN202 — union of two limiter types
    """Pick the limiter backend from settings (O-8). Redis when configured and
    reachable-at-construction; otherwise the in-process fallback."""
    settings = CommonSettings()
    if settings.rate_limit_backend == "redis":
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url)
            return RedisRateLimiter(max_requests=max_requests, window_s=window_s, client=client)
        except Exception:  # noqa: BLE001 — bad url / missing driver: degrade to in-memory
            logger.warning("rate limiter: redis backend requested but unavailable, using in-memory")
    return RateLimiter(max_requests=max_requests, window_s=window_s)


def rate_limit(max_requests: int, window_s: float) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing ``max_requests`` per ``window_s`` per IP.

    Attach via ``dependencies=[Depends(rate_limit(...))]`` so endpoint signatures
    stay unchanged. The backend limiter is built lazily on first request (so no
    Redis connection is opened at import time) and reused for the route's lifetime.
    """
    holder: dict[str, object] = {}

    async def _dep(request: Request) -> None:
        limiter = holder.get("limiter")
        if limiter is None:
            limiter = _build_limiter(max_requests, window_s)
            holder["limiter"] = limiter
        key = request.client.host if request.client else "unknown"
        verdict = limiter.check(key)  # type: ignore[attr-defined]
        allowed = await verdict if isawaitable(verdict) else verdict
        if not allowed:
            raise ApiError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                message="Too many requests. Please slow down and try again shortly.",
            )

    return _dep
