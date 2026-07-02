"""Shared async HTTP client factory tuned for low-latency provider calls.

The default httpx keepalive_expiry is 5s — longer than a turn's think-time, so
between turns the pooled TLS connection to Groq/NVIDIA is dropped and the next
call pays a fresh TLS+TCP handshake (a full extra round-trip to the US). We hold
connections open far longer and enable HTTP/2 (multiplexing for any concurrent
fan-out), so warm connections are reused across turns.
"""

from __future__ import annotations

import httpx

# Hold pooled connections well beyond inter-turn think-time so the TLS handshake
# is paid once per session, not once per turn.
_KEEPALIVE_EXPIRY_S = 300.0
_MAX_KEEPALIVE = 20
_MAX_CONNECTIONS = 100


def make_async_client(*, timeout_s: float, http2: bool = True) -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_keepalive_connections=_MAX_KEEPALIVE,
        max_connections=_MAX_CONNECTIONS,
        keepalive_expiry=_KEEPALIVE_EXPIRY_S,
    )
    try:
        return httpx.AsyncClient(timeout=timeout_s, limits=limits, http2=http2)
    except ImportError:
        # `h2` not installed → fall back to HTTP/1.1 keep-alive (still pooled).
        return httpx.AsyncClient(timeout=timeout_s, limits=limits)
