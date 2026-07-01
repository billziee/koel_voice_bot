"""Per-IP sliding-window rate limiting (M, PRD §08 abuse controls).

A dependency-free in-process limiter applied to auth + session-creation endpoints
so credential stuffing / session-spam is throttled. Single-worker scope is fine
for the pilot; a multi-worker deployment should swap in slowapi + Redis so the
window is shared across processes (documented tradeoff).
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from fastapi import Request, status

from koel.common.errors import ApiError


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

    def check(self, key: str) -> bool:
        """Record a hit; return True if within the limit, False if it should be rejected."""
        now = self._clock()
        bucket = [t for t in self._hits[key] if now - t < self._window]
        if len(bucket) >= self._max:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True


def rate_limit(max_requests: int, window_s: float) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing ``max_requests`` per ``window_s`` per IP.

    Attach via ``dependencies=[Depends(rate_limit(...))]`` so endpoint signatures
    stay unchanged. One limiter instance is bound per decorated route.
    """
    limiter = RateLimiter(max_requests=max_requests, window_s=window_s)

    async def _dep(request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        if not limiter.check(key):
            raise ApiError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="rate_limited",
                message="Too many requests. Please slow down and try again shortly.",
            )

    return _dep
