"""Live-call monitor for supervisor oversight (3.3).

A read-only registry of in-flight calls: the WS handler opens an entry when a
call starts, tees each transcript + call_state frame into a capped recent-events
buffer, and closes it on teardown. The operator API reads it so a supervisor can
watch live calls (transcript, status, elapsed) without touching the call.

In-process by default (dormant, like every other cross-instance feature in this
codebase) — reflects only calls served by THIS process. Pass a ``redis_client``
(a ``redis.asyncio.Redis``, or any test double exposing the same
get/set/delete/sadd/srem/smembers surface) to share the view across every
worker/instance instead: entries are written through to Redis with a bounded
TTL, and ``list_active``/``get`` read from there, so a supervisor sees calls
served by ANY process, not just the one their request happened to land on.

``redis_client`` is typed ``Any`` rather than a custom Protocol: redis-py's own
stubs use deliberately permissive parameter/return unions (they cover both the
sync and async clients, and don't encode ``decode_responses`` in the return
type), so a strict structural Protocol fights the library's own typing more
than it helps — the real contract is exercised by tests, not the type checker.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_MAX_EVENTS = 50  # recent transcript events retained per call (bounds memory)
_KEY_PREFIX = "koel:monitor:"
_ACTIVE_SET_KEY = "koel:monitor:active"
# A call is torn down (close()) on every normal/abnormal exit path, but a
# process crash would leak the Redis entry forever without a TTL floor.
_ENTRY_TTL_S = 6 * 60 * 60


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class MonitorEntry:
    session_id: str
    bot_id: str
    org_id: str | None
    status: str = "connecting"
    turn_count: int = 0
    opened_at: str = field(default_factory=_now_iso)
    last_at: str = field(default_factory=_now_iso)
    events: list[dict] = field(default_factory=list)  # recent {speaker, text, at}


def _summary(e: MonitorEntry) -> dict:
    return {
        "session_id": e.session_id,
        "bot_id": e.bot_id,
        "status": e.status,
        "turn_count": e.turn_count,
        "opened_at": e.opened_at,
        "last_at": e.last_at,
    }


class LiveMonitor:
    """Live view of in-flight calls — in-process, or Redis-shared across the
    fleet when constructed with a ``redis_client``."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._calls: dict[str, MonitorEntry] = {}  # used only when redis_client is None

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def _save(self, redis_client: Any, entry: MonitorEntry) -> None:
        await redis_client.set(self._key(entry.session_id), json.dumps(asdict(entry)), ex=_ENTRY_TTL_S)

    async def _load(self, redis_client: Any, session_id: str) -> MonitorEntry | None:
        raw = await redis_client.get(self._key(session_id))
        return MonitorEntry(**json.loads(raw)) if raw else None

    async def open(self, session_id: str, bot_id: str, org_id: str | None) -> None:
        entry = MonitorEntry(session_id=session_id, bot_id=bot_id, org_id=org_id)
        if self._redis is None:
            self._calls[session_id] = entry
            return
        await self._save(self._redis, entry)
        await self._redis.sadd(_ACTIVE_SET_KEY, session_id)

    async def close(self, session_id: str) -> None:
        if self._redis is None:
            self._calls.pop(session_id, None)
            return
        await self._redis.delete(self._key(session_id))
        await self._redis.srem(_ACTIVE_SET_KEY, session_id)

    async def set_status(self, session_id: str, status: str) -> None:
        if self._redis is None:
            entry = self._calls.get(session_id)
            if entry is not None:
                entry.status = status
                entry.last_at = _now_iso()
            return
        entry = await self._load(self._redis, session_id)
        if entry is None:
            return
        entry.status = status
        entry.last_at = _now_iso()
        await self._save(self._redis, entry)

    async def record_turn(self, session_id: str, speaker: str, text: str) -> None:
        entry: MonitorEntry | None
        if self._redis is None:
            entry = self._calls.get(session_id)
        else:
            entry = await self._load(self._redis, session_id)
        if entry is None:
            return
        entry.turn_count += 1
        entry.events.append({"speaker": speaker, "text": text, "at": _now_iso()})
        if len(entry.events) > _MAX_EVENTS:
            entry.events = entry.events[-_MAX_EVENTS:]
        entry.last_at = _now_iso()
        if self._redis is not None:
            await self._save(self._redis, entry)

    async def list_active(self, org_id: str | None) -> list[dict]:
        """Active calls, newest activity first; org-scoped when the caller is bound."""
        entries: list[MonitorEntry]
        if self._redis is None:
            entries = list(self._calls.values())
        else:
            session_ids = await self._redis.smembers(_ACTIVE_SET_KEY)
            loaded = [await self._load(self._redis, sid) for sid in session_ids]
            entries = [e for e in loaded if e is not None]
        entries = [e for e in entries if org_id is None or e.org_id == org_id]
        entries.sort(key=lambda e: e.last_at, reverse=True)
        return [_summary(e) for e in entries]

    async def get(self, session_id: str, org_id: str | None) -> dict | None:
        entry = (
            self._calls.get(session_id)
            if self._redis is None
            else await self._load(self._redis, session_id)
        )
        if entry is None or (org_id is not None and entry.org_id != org_id):
            return None
        return {**_summary(entry), "events": list(entry.events)}
