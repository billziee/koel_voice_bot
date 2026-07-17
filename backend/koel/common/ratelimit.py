"""Per-IP rate limiting (M, PRD §08 abuse controls).

Two interchangeable backends behind one FastAPI dependency (O-8):

- ``RateLimiter`` — dependency-free in-process fixed window. Correct for a single
  worker (dev / tests / the pilot), but its counters live in one process, so with
  ``uvicorn --workers >1`` or a second instance the effective limit multiplies by
  the process count and resets on deploy.
- ``RedisRateLimiter`` — the same fixed window shared across all workers and
  instances via an atomic Redis ``INCR`` + ``EXPIRE NX``. Selected by
  ``CommonSettings.rate_limit_backend == "redis"``.

The FastAPI dependency shape (``dependencies=[Depends(rate_limit(...))]``) is
unchanged; the backend is chosen from settings at first use, with in-memory as
the fallback. Each route passes a distinct ``name`` so its quota lives in its own
Redis namespace, and pre-auth routes can opt into fail-closed behaviour.
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

    Failure mode is controlled by ``fail_open`` (default ``True``): if Redis is
    unreachable the hit is allowed. A rate limiter is defence-in-depth on already
    authenticated endpoints, so an infra blip must not turn those into an outage.
    Pre-auth, brute-force-sensitive endpoints (login) pass ``fail_open=False`` so a
    Redis outage rejects the request rather than silently removing throttling. The
    event is logged either way.

    ``namespace`` discriminates one route's quota from another when several
    limiters share the same Redis: the key is ``namespace + client_ip``, so a
    per-route namespace (e.g. ``koel:rl:login:``) keeps each endpoint's counter
    independent instead of cross-contaminating one bucket per IP.
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_s: float,
        client,  # noqa: ANN001 — redis client type varies
        namespace: str = "koel:rl:",
        fail_open: bool = True,
    ) -> None:
        self._max = max_requests
        self._window = max(1, int(window_s))
        self._redis = client
        self._ns = namespace
        self._fail_open = fail_open

    async def check(self, key: str) -> bool:
        rk = f"{self._ns}{key}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(rk)
                # EXPIRE ... NX sets the TTL only when the key has none yet, so the
                # fixed window starts at the first request and isn't reset on every
                # hit (which would let sustained traffic keep the counter alive and
                # lock users out permanently). The NX flag needs Redis >= 7.0 — which
                # the project pins everywhere (docker-compose + CI = redis:7-alpine);
                # do not run this against an older server.
                pipe.expire(rk, self._window, nx=True)
                count, _ = await pipe.execute()
        except Exception:  # noqa: BLE001 — Redis unreachable: honor the fail_open policy
            logger.warning(
                "rate limiter: redis unavailable, %s request (fail-%s)",
                "allowing" if self._fail_open else "rejecting",
                "open" if self._fail_open else "closed",
            )
            return self._fail_open
        return int(count) <= self._max


def _build_limiter(
    max_requests: int,
    window_s: float,
    name: str,
    fail_open: bool,
):  # noqa: ANN202 — union of two limiter types
    """Pick the limiter backend from settings (O-8). Redis when configured and
    reachable-at-construction; otherwise the in-process fallback.

    ``name`` becomes a per-route Redis namespace so each endpoint's quota is
    independent. The in-memory ``RateLimiter`` is already per-instance isolated, so
    ``name``/``fail_open`` only affect the Redis backend."""
    settings = CommonSettings()
    if settings.rate_limit_backend == "redis":
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url)
            return RedisRateLimiter(
                max_requests=max_requests,
                window_s=window_s,
                client=client,
                namespace=f"koel:rl:{name}:",
                fail_open=fail_open,
            )
        except Exception:  # noqa: BLE001 — bad url / missing driver: degrade to in-memory
            logger.warning("rate limiter: redis backend requested but unavailable, using in-memory")
    return RateLimiter(max_requests=max_requests, window_s=window_s)


def rate_limit(
    max_requests: int,
    window_s: float,
    name: str = "default",
    fail_open: bool = True,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing ``max_requests`` per ``window_s`` per IP.

    Attach via ``dependencies=[Depends(rate_limit(...))]`` so endpoint signatures
    stay unchanged. The backend limiter is built lazily on first request (so no
    Redis connection is opened at import time) and reused for the route's lifetime.

    ``name`` isolates this route's quota from others sharing a Redis backend
    (distinct names → distinct key namespaces, so login/token/session counters
    never cross-contaminate). ``fail_open`` controls the Redis-error behaviour:
    ``True`` (default) allows the request on a Redis outage; set ``False`` on
    pre-auth, brute-force-sensitive routes so an outage rejects (429) instead of
    silently removing throttling. Both only affect the Redis backend.
    """
    holder: dict[str, object] = {}

    async def _dep(request: Request) -> None:
        limiter = holder.get("limiter")
        if limiter is None:
            limiter = _build_limiter(max_requests, window_s, name, fail_open)
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
