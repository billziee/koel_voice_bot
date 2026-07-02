"""RedisSessionStore (fake redis), auth WS guard, and DI wiring."""

from __future__ import annotations

from koel.common.config import CommonSettings
from koel.voicebot.session.models import SessionState
from koel.voicebot.session.store import RedisSessionStore


class FakeRedis:
    """Minimal async redis stand-in: get/set/expire/delete/ping/aclose."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):  # noqa: ANN001
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):  # noqa: ANN001
        return self.store.get(key)

    async def expire(self, key, ttl):  # noqa: ANN001
        if key in self.store:
            self.ttls[key] = ttl
            return 1
        return 0

    async def delete(self, key):  # noqa: ANN001
        self.store.pop(key, None)

    async def ping(self):
        return True

    async def aclose(self):
        pass


def _store() -> RedisSessionStore:
    s = RedisSessionStore("redis://localhost:6379/0", active_ttl=3600, grace_ttl=15)
    s._redis = FakeRedis()
    return s


async def test_store_save_load_roundtrip():
    s = _store()
    state = SessionState(session_id="rs1", bot_id="b", use_case="credit_card")
    await s.save(state)
    loaded = await s.load("rs1")
    assert loaded is not None and loaded.session_id == "rs1"
    assert await s.load("missing") is None


async def test_store_grace_and_restore_ttl():
    s = _store()
    await s.save(SessionState(session_id="rs2", bot_id="b", use_case="credit_card"))
    assert await s.begin_grace("rs2") is True
    assert s._redis.ttls["koel:session:rs2"] == 15  # grace window
    assert await s.restore_active_ttl("rs2") is True
    assert s._redis.ttls["koel:session:rs2"] == 3600
    # A missing key can't be graced.
    assert await s.begin_grace("gone") is False


async def test_store_delete_and_ping():
    s = _store()
    await s.save(SessionState(session_id="rs3", bot_id="b", use_case="credit_card"))
    await s.delete("rs3")
    assert await s.load("rs3") is None
    assert await s.ping() is True
    await s.close()


async def test_authenticate_websocket_closes_on_missing_token():
    from types import SimpleNamespace

    from koel.common.auth import authenticate_websocket

    closed = {}

    async def close(code=None, reason=None):  # noqa: ANN001
        closed["code"] = code

    ws = SimpleNamespace(headers={}, query_params={}, close=close)
    result = await authenticate_websocket(ws)
    assert result is None and closed["code"] == 1008


async def test_authenticate_websocket_accepts_valid_ws_token():
    from types import SimpleNamespace

    from koel.common.auth import authenticate_websocket, issue_token

    tok = issue_token("op", kind="ws", org_id="acme")
    ws = SimpleNamespace(headers={"sec-websocket-protocol": f"koel.v1, koel.token.{tok}"},
                         query_params={}, close=None)
    claims = await authenticate_websocket(ws)
    assert claims is not None and claims.sub == "op" and claims.org_id == "acme"


def test_build_runtime_wires_all_components():
    from koel.voicebot.config import get_voicebot_settings
    from koel.voicebot.deps import build_runtime

    rt = build_runtime(get_voicebot_settings())
    assert rt.orchestrator is not None and rt.bargein is not None and rt.filler is not None
    assert isinstance(rt.settings, CommonSettings)
