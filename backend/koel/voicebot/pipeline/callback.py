"""Scheduled-callback time parsing (2.5).

Turns a spoken deferral — "call me tomorrow at 5", "in two hours", "kal shaam
5 baje" — into an absolute UTC instant, deterministically and without a model
call, so it works even when the NLP provider is dormant (no key). The NLP layer
may also emit a structured ``at`` (ISO) directly; the orchestrator prefers that
and falls back to this parser on the raw transcript.

Assumptions (documented so they're auditable, PRD §11 — callbacks must land in
calling hours): times are interpreted in the campaign timezone (default
Asia/Kolkata). A bare hour with no am/pm below 8 is read as PM (a 5-o'clock
callback means 17:00, not 05:00 — business-hours heuristic); 8–11 stays AM. A
time that has already passed today rolls to the same time tomorrow unless a day
word ("tomorrow"/"kal") already pinned the date.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"

# Furthest out we'll schedule a parsed callback — a spoken "next month" is noise,
# not a real callback commitment.
_MAX_HORIZON = timedelta(days=14)
_PM_HEURISTIC_CUTOFF = 8  # bare hour < 8 (no am/pm) → PM

_TOMORROW = re.compile(r"\b(tomorrow|kal)\b")
_TODAY = re.compile(r"\b(today|aaj)\b")
# "in 2 hours", "in an hour", "in half an hour", "in 30 minutes/mins"
_REL_HOURS = re.compile(r"\bin\s+(an?|\d+|half an)\s+hours?\b")
_REL_MINUTES = re.compile(r"\bin\s+(\d+)\s+(?:minutes?|mins?)\b")
# "at 5", "at 5:30 pm", "5pm", "5 baje" (Hindi 'o'clock')
_CLOCK = re.compile(
    r"(?:\bat\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?:\s*baje)?", re.IGNORECASE
)
_WORD_HOURS = {"an": 1, "a": 1, "half an": 0}  # "half an hour" handled specially


def parse_callback_time(
    text: str, now: datetime, tz: str = DEFAULT_TZ
) -> datetime | None:
    """Parse a callback request into a future UTC datetime, or ``None`` if the
    text carries no recognisable time. ``now`` is supplied by the caller so the
    result is fully reproducible (and testable) — no hidden clock."""
    if not text:
        return None
    lowered = text.lower()
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)

    # 1. Relative offsets take precedence — they're unambiguous.
    rel = _relative_offset(lowered)
    if rel is not None:
        target = local_now + rel
        return _finalize(target, now, local_now)

    # 2. Absolute clock time, optionally pinned to today/tomorrow.
    clock = _clock_time(lowered)
    if clock is None:
        return None
    hour, minute = clock
    base = local_now.date()
    pinned_tomorrow = bool(_TOMORROW.search(lowered))
    if pinned_tomorrow:
        base = base + timedelta(days=1)
    target = datetime(base.year, base.month, base.day, hour, minute, tzinfo=zone)
    # Time already gone today and no explicit "tomorrow"/"today" → roll to tomorrow.
    if target <= local_now and not pinned_tomorrow and not _TODAY.search(lowered):
        target = target + timedelta(days=1)
    return _finalize(target, now, local_now)


def _relative_offset(text: str) -> timedelta | None:
    if "half an hour" in text:
        return timedelta(minutes=30)
    m = _REL_MINUTES.search(text)
    if m:
        return timedelta(minutes=int(m.group(1)))
    m = _REL_HOURS.search(text)
    if m:
        token = m.group(1)
        hours = _WORD_HOURS.get(token, None)
        if hours is None:
            try:
                hours = int(token)
            except ValueError:
                return None
        return timedelta(hours=hours)
    return None


def _clock_time(text: str) -> tuple[int, int] | None:
    m = _CLOCK.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if hour > 23 or minute > 59:
        return None
    meridiem = (m.group(3) or "").replace(".", "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and 0 < hour < _PM_HEURISTIC_CUTOFF:
        hour += 12  # "at 5" → 17:00 (business-hours heuristic)
    return hour, minute


def _finalize(
    target: datetime, now: datetime, local_now: datetime
) -> datetime | None:
    """Reject the past and the absurdly-far-future; return a UTC instant."""
    if target <= local_now or target - local_now > _MAX_HORIZON:
        return None
    return target.astimezone(now.tzinfo) if now.tzinfo else target.astimezone(ZoneInfo("UTC"))
