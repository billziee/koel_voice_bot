"""Redis session store with TTL (PRD §08).

The grace-TTL mechanic (PRD §05): on socket disconnect the session TTL is
shortened to 15s instead of being deleted, so a reconnect with the same
Session ID restores transcript + script position; if no reconnect arrives, the
key expires and the call is finalised as ``call_dropped``.
"""

from __future__ import annotations

import redis.asyncio as redis

from koel.voicebot.session.models import CallStatus, SessionState

_KEY_PREFIX = "koel:session:"


class RedisSessionStore:
    def __init__(self, redis_url: str, *, active_ttl: int, grace_ttl: int) -> None:
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._active_ttl = active_ttl
        self._grace_ttl = grace_ttl

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def save(self, state: SessionState) -> None:
        """Persist with the active TTL (called on every mutation)."""
        await self._redis.set(
            self._key(state.session_id),
            state.model_dump_json(),
            ex=self._active_ttl,
        )

    async def load(self, session_id: str) -> SessionState | None:
        raw = await self._redis.get(self._key(session_id))
        return SessionState.model_validate_json(raw) if raw else None

    async def is_live(self, session_id: str) -> bool:
        """Whether a session is still going, from any process's perspective —
        the cross-process liveness signal for the escalation hub's pub/sub
        path (Redis pub/sub itself has no delivery ack, so this is the best
        available "did that land somewhere" check)."""
        state = await self.load(session_id)
        return state is not None and state.status != CallStatus.ended

    async def begin_grace(self, session_id: str) -> bool:
        """Shorten TTL to the 15s grace window on disconnect. Returns False if gone."""
        return bool(await self._redis.expire(self._key(session_id), self._grace_ttl))

    async def restore_active_ttl(self, session_id: str) -> bool:
        """Re-extend TTL when a socket re-binds within the grace window."""
        return bool(await self._redis.expire(self._key(session_id), self._active_ttl))

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        await self._redis.aclose()
