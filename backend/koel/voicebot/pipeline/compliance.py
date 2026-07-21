"""Outbound-dial compliance gates (PRD §11) shared by every origination path.

Currently one gate: RBI fair-practice calling hours. Pulled out of
``api/sessions.py`` (the original, HTTP-triggered outbound path) so the
campaign dialer worker enforces the exact same window — a dial placed by the
worker at 2am must be refused exactly like one attempted via the API, not
have its own, possibly-drifted copy of the same check.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def is_within_calling_hours(
    *, enforce: bool, tz_name: str, start_hour: int, end_hour: int, now: datetime | None = None
) -> bool:
    """True iff a dial is permitted right now. Always True when ``enforce`` is
    off (demos run any time). The window is evaluated in ``tz_name`` (IST by
    default) — NOT the server's clock, which is UTC on the cloud host (a
    9-21 window in UTC would be 14:30-02:30 IST, refusing legitimate daytime
    dials). A bad timezone string fails safe to UTC rather than raising."""
    if not enforce:
        return True
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — bad tz string → fail safe to UTC
        tz = ZoneInfo("UTC")
    hour = (now or datetime.now(tz)).astimezone(tz).hour
    return start_hour <= hour < end_hour
