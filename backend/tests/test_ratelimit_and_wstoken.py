"""Rate limiter + WS token transport (M)."""

from __future__ import annotations

from types import SimpleNamespace

from koel.common.auth import websocket_token
from koel.common.ratelimit import RateLimiter


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_rate_limiter_blocks_beyond_cap_then_recovers():
    clk = _Clock()
    rl = RateLimiter(max_requests=3, window_s=60, clock=clk)
    assert [rl.check("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert rl.check("1.2.3.4") is False  # 4th within window → blocked
    clk.t = 61  # window elapsed
    assert rl.check("1.2.3.4") is True


def test_rate_limiter_is_per_key():
    rl = RateLimiter(max_requests=1, window_s=60)
    assert rl.check("a") is True
    assert rl.check("b") is True  # different IP unaffected
    assert rl.check("a") is False


def _ws(headers: dict[str, str], query: dict[str, str] | None = None):
    return SimpleNamespace(headers=headers, query_params=query or {})


def test_ws_token_prefers_subprotocol():
    ws = _ws(
        {"sec-websocket-protocol": "koel.v1, koel.token.JWT123"},
        {"token": "from_query"},
    )
    assert websocket_token(ws) == "JWT123"


def test_ws_token_falls_back_to_authorization_then_query():
    assert websocket_token(_ws({"authorization": "Bearer HDR"})) == "HDR"
    assert websocket_token(_ws({}, {"token": "Q"})) == "Q"
    assert websocket_token(_ws({})) is None
