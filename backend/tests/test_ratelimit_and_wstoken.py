"""Rate limiter + WS token transport (M)."""

from __future__ import annotations

from types import SimpleNamespace

from koel.common.auth import websocket_token
from koel.common.ratelimit import RateLimiter, RedisRateLimiter


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


# --- Redis-backed limiter (O-8) ----------------------------------------------
# A faithful in-memory stand-in for the tiny slice of redis.asyncio we use:
# a transactional pipeline running INCR + EXPIRE key window NX.


class _FakeRedisPipe:
    def __init__(self, store: _FakeRedis) -> None:
        self._store = store
        self._ops: list = []

    async def __aenter__(self) -> _FakeRedisPipe:
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    def incr(self, key: str) -> _FakeRedisPipe:
        self._ops.append(("incr", key))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _FakeRedisPipe:
        self._ops.append(("expire", key, seconds, nx))
        return self

    async def execute(self) -> list:
        out: list = []
        for op in self._ops:
            if op[0] == "incr":
                k = op[1]
                self._store.counts[k] = self._store.counts.get(k, 0) + 1
                out.append(self._store.counts[k])
            else:
                _, k, secs, nx = op
                if nx and k in self._store.ttl:
                    out.append(False)  # NX: don't overwrite an existing TTL
                else:
                    self._store.ttl[k] = secs
                    out.append(True)
        self._ops = []
        return out


class _FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakeRedisPipe:
        return _FakeRedisPipe(self)

    def elapse(self, key: str) -> None:
        """Test helper: simulate the window TTL expiring."""
        self.counts.pop(key, None)
        self.ttl.pop(key, None)


class _BrokenRedis:
    def pipeline(self, transaction: bool = True) -> _FakeRedisPipe:
        raise ConnectionError("redis down")


async def test_redis_limiter_blocks_beyond_cap_and_is_per_key():
    fake = _FakeRedis()
    rl = RedisRateLimiter(max_requests=3, window_s=60, client=fake)
    assert [await rl.check("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert await rl.check("1.2.3.4") is False  # 4th within window → blocked
    assert await rl.check("5.6.7.8") is True   # different IP unaffected
    # TTL stamped exactly once (EXPIRE NX), on the first hit.
    assert fake.ttl["koel:rl:1.2.3.4"] == 60


async def test_redis_limiter_recovers_after_window_expires():
    fake = _FakeRedis()
    rl = RedisRateLimiter(max_requests=1, window_s=60, client=fake)
    assert await rl.check("1.2.3.4") is True
    assert await rl.check("1.2.3.4") is False
    fake.elapse("koel:rl:1.2.3.4")  # window TTL elapses
    assert await rl.check("1.2.3.4") is True  # counter reset → allowed again


async def test_redis_limiter_fails_open_when_redis_unavailable():
    # A rate limiter is defence-in-depth; a Redis outage must not block auth.
    rl = RedisRateLimiter(max_requests=1, window_s=60, client=_BrokenRedis())
    assert await rl.check("1.2.3.4") is True
    assert await rl.check("1.2.3.4") is True  # still allowed — never blocks on error


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
