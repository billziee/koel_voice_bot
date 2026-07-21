"""Live bridge for the human-handoff (3.1 Phase A).

When a call escalates it parks in ``waiting_agent`` and registers a pair of
callbacks here; the operator REST API (``koel.voicebot.api.operators``) looks the
session up by id and pushes the operator's typed reply back to the caller —
spoken via the normal TTS path — or resolves (ends) the handoff.

Local registration is always in-process (a live callback is a Python closure
bound to one process's WebSocket — it fundamentally cannot cross a process
boundary). Dormant by default: with no ``redis_client``, ``deliver``/``resolve``
only reach a call registered on THIS process, matching the single-combined-
deployment reality KOEL actually runs today. Pass a ``redis_client`` to also
publish to a channel other processes listen on — the cross-process case for a
multi-worker/multi-instance deployment (a follow-up, previously noted in
docs/FUTURE_PROCEEDINGS.md, now implemented).

Pub/sub has no delivery acknowledgement, so the cross-process "did this work"
signal is instead read from the shared Redis session store: if the session is
still live there (not ended), the publish is treated as delivered — whichever
process actually holds the socket will pick the message up via ``run_listener``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from koel.common.logging import get_logger

logger = get_logger("koel.voicebot.escalation")

Deliver = Callable[[str], Awaitable[None]]
Resolve = Callable[[], Awaitable[None]]

_CHANNEL_PREFIX = "koel:escalation:"

# ``redis_client`` (a redis.asyncio.Redis, or any test double exposing
# publish()/pubsub()) is typed Any rather than a custom Protocol — redis-py's
# own stubs use deliberately permissive unions that a strict structural
# Protocol fights more than it helps (see monitor.py's docstring for the same
# reasoning). The real contract — publish a JSON string, get one back via
# pubsub().psubscribe()/get_message() — is exercised by tests, not mypy.


class SessionLivenessCheck(Protocol):
    """The one thing the hub needs from the session store — kept narrow so
    escalation.py doesn't depend on the whole RedisSessionStore/SessionState
    surface, just "is this session still going"."""

    async def is_live(self, session_id: str) -> bool: ...


@dataclass
class _LiveHandoff:
    deliver: Deliver  # speak an operator's reply to the caller
    resolve: Resolve  # end the handoff + the call


class EscalationHub:
    """Maps a held call's ``session_id`` to its live operator callbacks."""

    def __init__(
        self,
        redis_client: Any | None = None,
        liveness: SessionLivenessCheck | None = None,
    ) -> None:
        self._live: dict[str, _LiveHandoff] = {}
        self._redis = redis_client
        self._liveness = liveness

    def register(self, session_id: str, *, deliver: Deliver, resolve: Resolve) -> None:
        self._live[session_id] = _LiveHandoff(deliver=deliver, resolve=resolve)

    def unregister(self, session_id: str) -> None:
        self._live.pop(session_id, None)

    def is_live(self, session_id: str) -> bool:
        return session_id in self._live

    async def deliver(self, session_id: str, text: str) -> bool:
        """Speak an operator reply to the caller. Local fast path when this
        process holds the live socket; otherwise (Redis-backed only) publish
        for whichever process does. False if the session isn't live
        anywhere."""
        handoff = self._live.get(session_id)
        if handoff is not None:
            await handoff.deliver(text)
            return True
        return await self._publish(session_id, {"action": "deliver", "text": text})

    async def resolve(self, session_id: str) -> bool:
        """End the handoff + the caller's call. False if not live anywhere."""
        handoff = self._live.get(session_id)
        if handoff is not None:
            await handoff.resolve()
            return True
        return await self._publish(session_id, {"action": "resolve"})

    async def _publish(self, session_id: str, payload: dict) -> bool:
        if self._redis is None:
            return False  # dormant: no cross-process channel configured
        await self._redis.publish(f"{_CHANNEL_PREFIX}{session_id}", json.dumps(payload))
        if self._liveness is None:
            return False
        return await self._liveness.is_live(session_id)

    async def _dispatch(self, session_id: str, raw: str) -> None:
        """Apply one cross-process message to THIS process's local handoff,
        if it has one — a message for a session live on another process is
        silently ignored there (exactly one process ever holds the socket)."""
        handoff = self._live.get(session_id)
        if handoff is None:
            return
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning(
                "escalation_message_malformed",
                extra={"extra_fields": {"session_id": session_id}},
            )
            return
        if payload.get("action") == "deliver":
            await handoff.deliver(str(payload.get("text", "")))
        elif payload.get("action") == "resolve":
            await handoff.resolve()

    async def run_listener(self) -> None:
        """Cross-process fan-in — start once at app startup (dormant unless
        constructed with a ``redis_client``) and let it run until cancelled."""
        if self._redis is None:
            return
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(f"{_CHANNEL_PREFIX}*")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    # Real redis-py's get_message already blocks up to
                    # `timeout` on socket I/O (yielding to the event loop
                    # naturally); this sleep is cheap insurance against a
                    # future client implementation that returns instantly.
                    await asyncio.sleep(0.05)
                    continue
                channel = str(message.get("channel", ""))
                session_id = channel[len(_CHANNEL_PREFIX):]
                if not session_id:
                    continue
                await self._dispatch(session_id, str(message.get("data", "")))
        finally:
            await pubsub.punsubscribe()
            await pubsub.aclose()
