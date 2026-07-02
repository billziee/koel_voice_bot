"""The combined app's self-ping keeps an idle-spin-down host warm.

Covers the two properties that matter: it pings ``<url>/health``, and a failed
ping never kills the loop (transient network / brief downtime between demos).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from koel import combined


class _FakeClient:
    """Async-context httpx stand-in whose ``get`` delegates to a callback."""

    def __init__(self, on_get) -> None:  # noqa: ANN001
        self._on_get = on_get

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc) -> bool:  # noqa: ANN002
        return False

    async def get(self, url: str):  # noqa: ANN201
        return await self._on_get(url)


def _patch_client(monkeypatch, on_get) -> None:  # noqa: ANN001
    # _keepalive_loop does `from koel.common.http import make_async_client` at call
    # time, so patch it on the source module.
    monkeypatch.setattr("koel.common.http.make_async_client", lambda **_k: _FakeClient(on_get))


async def test_keepalive_pings_health_then_cancels_cleanly(monkeypatch):
    seen = asyncio.Event()
    calls: list[str] = []

    async def on_get(url: str):
        calls.append(url)
        seen.set()
        return SimpleNamespace(status_code=200)

    _patch_client(monkeypatch, on_get)
    task = asyncio.create_task(combined._keepalive_loop("https://x.onrender.com/", 0))  # noqa: SLF001

    await asyncio.wait_for(seen.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls[0] == "https://x.onrender.com/health"  # trailing slash trimmed, /health appended


async def test_keepalive_survives_a_failed_ping(monkeypatch):
    reached_second = asyncio.Event()
    calls = 0

    async def on_get(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom")  # transient outage on the first tick
        reached_second.set()
        return SimpleNamespace(status_code=200)

    _patch_client(monkeypatch, on_get)
    task = asyncio.create_task(combined._keepalive_loop("https://x/", 0))  # noqa: SLF001

    # If the loop died on the first error it would never reach the second ping.
    await asyncio.wait_for(reached_second.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 2


def test_keepalive_disabled_by_default():
    # No KOEL_KEEPALIVE_URL ⇒ no self-ping (never runs locally / in CI).
    from koel.common.config import CommonSettings

    assert CommonSettings().keepalive_url is None
