"""In-process live-call monitor for supervisor oversight (3.3).

A read-only registry of in-flight calls: the WS handler opens an entry when a
call starts, tees each transcript + call_state frame into a capped recent-events
buffer, and closes it on teardown. The operator API reads it so a supervisor can
watch live calls (transcript, status, elapsed) without touching the call.

In-process, like the escalation hub (koel.voicebot.escalation) — it therefore
reflects calls served by THIS process. A multi-worker/multi-instance deployment
would fan this out over Redis pub/sub (a follow-up, noted in docs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

_MAX_EVENTS = 50  # recent transcript events retained per call (bounds memory)


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
    """Live view of in-flight calls served by this process."""

    def __init__(self) -> None:
        self._calls: dict[str, MonitorEntry] = {}

    def open(self, session_id: str, bot_id: str, org_id: str | None) -> None:
        self._calls[session_id] = MonitorEntry(session_id=session_id, bot_id=bot_id, org_id=org_id)

    def close(self, session_id: str) -> None:
        self._calls.pop(session_id, None)

    def set_status(self, session_id: str, status: str) -> None:
        entry = self._calls.get(session_id)
        if entry is not None:
            entry.status = status
            entry.last_at = _now_iso()

    def record_turn(self, session_id: str, speaker: str, text: str) -> None:
        entry = self._calls.get(session_id)
        if entry is None:
            return
        entry.turn_count += 1
        entry.events.append({"speaker": speaker, "text": text, "at": _now_iso()})
        if len(entry.events) > _MAX_EVENTS:
            entry.events = entry.events[-_MAX_EVENTS:]
        entry.last_at = _now_iso()

    def list_active(self, org_id: str | None) -> list[dict]:
        """Active calls, newest activity first; org-scoped when the caller is bound."""
        entries = [
            e for e in self._calls.values() if org_id is None or e.org_id == org_id
        ]
        entries.sort(key=lambda e: e.last_at, reverse=True)
        return [_summary(e) for e in entries]

    def get(self, session_id: str, org_id: str | None) -> dict | None:
        entry = self._calls.get(session_id)
        if entry is None or (org_id is not None and entry.org_id != org_id):
            return None
        return {**_summary(entry), "events": list(entry.events)}
