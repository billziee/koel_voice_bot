"""In-process live bridge for the human-handoff (3.1 Phase A).

When a call escalates it parks in ``waiting_agent`` and registers a pair of
callbacks here; the operator REST API (``koel.voicebot.api.operators``) looks the
session up by id and pushes the operator's typed reply back to the caller —
spoken via the normal TTS path — or resolves (ends) the handoff.

The durable queue is the ``escalation_requests`` table; this hub only holds the
live socket callbacks, so the operator API and the caller's WebSocket must share
a process. That holds in the combined single-process deployment KOEL actually
runs; a multi-worker/multi-instance deployment would back this with Redis
pub/sub instead (a follow-up, noted in docs/FUTURE_PROCEEDINGS.md).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Deliver = Callable[[str], Awaitable[None]]
Resolve = Callable[[], Awaitable[None]]


@dataclass
class _LiveHandoff:
    deliver: Deliver  # speak an operator's reply to the caller
    resolve: Resolve  # end the handoff + the call


class EscalationHub:
    """Maps a held call's ``session_id`` to its live operator callbacks."""

    def __init__(self) -> None:
        self._live: dict[str, _LiveHandoff] = {}

    def register(self, session_id: str, *, deliver: Deliver, resolve: Resolve) -> None:
        self._live[session_id] = _LiveHandoff(deliver=deliver, resolve=resolve)

    def unregister(self, session_id: str) -> None:
        self._live.pop(session_id, None)

    def is_live(self, session_id: str) -> bool:
        return session_id in self._live

    async def deliver(self, session_id: str, text: str) -> bool:
        """Speak an operator reply to the caller. False if the call isn't live
        here (already ended, or on a different process)."""
        handoff = self._live.get(session_id)
        if handoff is None:
            return False
        await handoff.deliver(text)
        return True

    async def resolve(self, session_id: str) -> bool:
        """End the handoff + the caller's call. False if not live here."""
        handoff = self._live.get(session_id)
        if handoff is None:
            return False
        await handoff.resolve()
        return True
