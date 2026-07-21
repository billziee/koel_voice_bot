"""Redis-backed LiveMonitor + EscalationHub (multi-worker/multi-instance
follow-up from docs/FUTURE_PROCEEDINGS.md, now implemented).

Uses a small in-memory fake satisfying the narrow client surface each class
actually calls — no real Redis needed, and a SHARED fake broker across two
independent EscalationHub instances simulates two separate worker processes.
"""

from __future__ import annotations

import asyncio
import json

from koel.voicebot.escalation import EscalationHub
from koel.voicebot.monitor import LiveMonitor


class FakeRedisBroker:
    """In-memory stand-in for redis.asyncio.Redis, scoped to exactly the
    surface LiveMonitor/EscalationHub use. Share ONE instance across multiple
    hub/monitor objects to simulate cross-process behavior in a single test
    process."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self._kv[key] = value

    async def delete(self, key: str) -> None:
        self._kv.pop(key, None)

    async def sadd(self, key: str, member: str) -> None:
        self._sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self._sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)


class _FakePubSub:
    def __init__(self, broker: FakeRedisBroker) -> None:
        self._broker = broker
        self._prefix = ""
        self._next_index = 0

    async def psubscribe(self, pattern: str) -> None:
        self._prefix = pattern[:-1] if pattern.endswith("*") else pattern

    async def get_message(self, *, ignore_subscribe_messages: bool = True, timeout: float | None = None):
        # Real redis-py awaits actual socket I/O here, which naturally yields
        # to the event loop while nothing has arrived. This fake has no I/O to
        # await, so it must yield explicitly — otherwise run_listener's
        # `while True: await get_message(); continue` never suspends and
        # starves every other task (a real deadlock, not just slow).
        await asyncio.sleep(0)
        # Only look at messages published AFTER this subscriber attached —
        # mirrors real pub/sub (no replay of history).
        while self._next_index < len(self._broker.published):
            channel, data = self._broker.published[self._next_index]
            self._next_index += 1
            if channel.startswith(self._prefix):
                return {"type": "pmessage", "channel": channel, "data": data}
        return None

    async def punsubscribe(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class FakeLivenessCheck:
    def __init__(self, live_sessions: set[str] | None = None) -> None:
        self.live_sessions = live_sessions or set()

    async def is_live(self, session_id: str) -> bool:
        return session_id in self.live_sessions


# --- LiveMonitor, Redis-backed -----------------------------------------------


async def test_redis_backed_monitor_round_trips_through_the_client():
    broker = FakeRedisBroker()
    m = LiveMonitor(redis_client=broker)

    await m.open("s1", "bot_x", "org1")
    assert [c["session_id"] for c in await m.list_active("org1")] == ["s1"]

    await m.record_turn("s1", "bot", "Namaste")
    await m.set_status("s1", "processing")
    detail = await m.get("s1", "org1")
    assert detail["turn_count"] == 1
    assert detail["status"] == "processing"
    assert detail["events"][0]["text"] == "Namaste"

    await m.close("s1")
    assert await m.list_active("org1") == []
    assert await m.get("s1", "org1") is None


async def test_redis_backed_monitor_shared_across_two_instances():
    # The whole point: TWO separate LiveMonitor objects (one per "process"),
    # sharing the same broker, see each other's writes.
    broker = FakeRedisBroker()
    writer = LiveMonitor(redis_client=broker)
    reader = LiveMonitor(redis_client=broker)

    await writer.open("cross1", "bot_y", "orgX")
    await writer.record_turn("cross1", "customer", "hello from another process")

    assert [c["session_id"] for c in await reader.list_active("orgX")] == ["cross1"]
    detail = await reader.get("cross1", "orgX")
    assert detail["events"][0]["text"] == "hello from another process"


async def test_redis_backed_monitor_org_scoping():
    broker = FakeRedisBroker()
    m = LiveMonitor(redis_client=broker)
    await m.open("a", "bot", "orgA")
    await m.open("b", "bot", "orgB")
    assert [c["session_id"] for c in await m.list_active("orgA")] == ["a"]
    assert await m.get("a", "orgB") is None  # cross-tenant → hidden
    assert len(await m.list_active(None)) == 2


# --- EscalationHub, Redis-backed ---------------------------------------------


async def test_deliver_publishes_and_returns_liveness_when_not_local():
    broker = FakeRedisBroker()
    liveness = FakeLivenessCheck(live_sessions={"s1"})
    hub = EscalationHub(redis_client=broker, liveness=liveness)

    # Nothing registered locally — falls through to publish + liveness check.
    delivered = await hub.deliver("s1", "hello from the operator API")
    assert delivered is True
    assert len(broker.published) == 1
    channel, payload = broker.published[0]
    assert channel == "koel:escalation:s1"
    assert json.loads(payload) == {"action": "deliver", "text": "hello from the operator API"}

    # A session that isn't live anywhere → False, even though publish still fires.
    assert await hub.deliver("gone", "x") is False


async def test_resolve_publishes_resolve_action():
    broker = FakeRedisBroker()
    liveness = FakeLivenessCheck(live_sessions={"s1"})
    hub = EscalationHub(redis_client=broker, liveness=liveness)
    assert await hub.resolve("s1") is True
    channel, payload = broker.published[-1]
    assert channel == "koel:escalation:s1"
    assert json.loads(payload) == {"action": "resolve"}


async def test_local_handoff_short_circuits_before_publishing():
    # When THIS process holds the socket, deliver/resolve never touch Redis —
    # the fast local path, unaffected by liveness/pub-sub at all.
    broker = FakeRedisBroker()
    hub = EscalationHub(redis_client=broker, liveness=FakeLivenessCheck())
    delivered: list[str] = []

    async def _deliver(text: str) -> None:
        delivered.append(text)

    async def _resolve() -> None:
        pass

    hub.register("local1", deliver=_deliver, resolve=_resolve)
    assert await hub.deliver("local1", "hi") is True
    assert delivered == ["hi"]
    assert broker.published == []  # never published — local fast path


async def test_dormant_without_redis_client_returns_false():
    hub = EscalationHub()  # no redis_client — matches today's single-process reality
    assert await hub.deliver("nope", "x") is False
    assert await hub.resolve("nope") is False


async def test_cross_process_delivery_via_shared_broker():
    # The real end-to-end case: TWO EscalationHub instances (simulating two
    # worker processes) sharing one broker. Process A holds the live call;
    # process B (where the operator API request landed) calls deliver().
    broker = FakeRedisBroker()
    liveness = FakeLivenessCheck(live_sessions={"s1"})
    process_a = EscalationHub(redis_client=broker, liveness=liveness)
    process_b = EscalationHub(redis_client=broker, liveness=liveness)

    delivered: list[str] = []

    async def _deliver(text: str) -> None:
        delivered.append(text)

    async def _resolve() -> None:
        pass

    process_a.register("s1", deliver=_deliver, resolve=_resolve)

    listener = asyncio.create_task(process_a.run_listener())
    try:
        ok = await process_b.deliver("s1", "reaching across processes")
        assert ok is True
        # The listener polls with a short internal wait; give it a moment to
        # pick up the published message and invoke the local callback.
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert delivered == ["reaching across processes"]
    finally:
        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass


async def test_message_meant_for_another_session_is_ignored_locally():
    # process_a only holds "s1" — a message on "other-session" must not be
    # dispatched anywhere (there's no local handoff for it on this process).
    broker = FakeRedisBroker()
    hub = EscalationHub(redis_client=broker)
    delivered: list[str] = []

    async def _deliver(text: str) -> None:
        delivered.append(text)

    async def _resolve() -> None:
        pass

    hub.register("s1", deliver=_deliver, resolve=_resolve)
    await hub._dispatch("other-session", json.dumps({"action": "deliver", "text": "not for you"}))
    assert delivered == []


async def test_malformed_message_does_not_raise():
    hub = EscalationHub()
    hub.register("s1", deliver=lambda t: asyncio.sleep(0), resolve=lambda: asyncio.sleep(0))
    await hub._dispatch("s1", "not-json{{{")  # must not raise
