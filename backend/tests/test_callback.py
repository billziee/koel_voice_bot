"""Scheduled-callback parsing + persistence (2.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from koel.voicebot.campaigns import claim_due_attempts, schedule_callback
from koel.voicebot.pipeline.callback import parse_callback_time

# A fixed "now": 2026-07-12 10:00 UTC == 15:30 IST (Asia/Kolkata, +5:30).
_NOW = datetime(2026, 7, 12, 10, 0, 0, tzinfo=UTC)
_IST = ZoneInfo("Asia/Kolkata")


def _ist(dt: datetime | None) -> datetime | None:
    return dt.astimezone(_IST) if dt else None


# --- parser -----------------------------------------------------------------


def test_tomorrow_at_five_is_tomorrow_1700_ist():
    got = _ist(parse_callback_time("call me tomorrow at 5", _NOW))
    assert got is not None
    assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 7, 13, 17, 0)


def test_bare_evening_hour_defaults_to_pm():
    # "at 5" with no am/pm → 17:00 (business-hours heuristic), not 05:00.
    got = _ist(parse_callback_time("please call at 5", _NOW))
    assert got is not None and got.hour == 17


def test_explicit_am_is_respected_and_rolls_to_tomorrow():
    # 10:00 AM IST has already passed by 15:30 → same time tomorrow.
    got = _ist(parse_callback_time("call me at 10 am", _NOW))
    assert got is not None
    assert (got.day, got.hour) == (13, 10)


def test_relative_in_two_hours():
    got = parse_callback_time("call me back in 2 hours", _NOW)
    assert got == _NOW + timedelta(hours=2)


def test_relative_half_an_hour():
    got = parse_callback_time("try again in half an hour", _NOW)
    assert got == _NOW + timedelta(minutes=30)


def test_hinglish_kal_shaam_baje():
    # "kal" = tomorrow, "5 baje" = 5 o'clock → tomorrow 17:00 IST.
    got = _ist(parse_callback_time("kal shaam 5 baje call karna", _NOW))
    assert got is not None
    assert (got.day, got.hour) == (13, 17)


def test_no_time_returns_none():
    assert parse_callback_time("I am not interested at all", _NOW) is None
    assert parse_callback_time("", _NOW) is None


def test_absurdly_far_future_is_rejected():
    # Beyond the 14-day horizon → not a real callback commitment.
    assert parse_callback_time("in 400 hours", _NOW) is None


# --- persistence wrapper ----------------------------------------------------


async def test_schedule_callback_enqueues_future_dial(test_db):
    at = _NOW + timedelta(hours=3)
    attempt_id = await schedule_callback(
        org_id="org1", customer_id="c1", phone="+911234567890", at=at, now=_NOW
    )
    assert attempt_id is not None
    # It's scheduled for later, so it isn't due yet.
    assert await claim_due_attempts(limit=10, now=_NOW) == []
    # ...but it is due once we reach that time.
    due = await claim_due_attempts(limit=10, now=at + timedelta(seconds=1))
    assert len(due) == 1 and due[0]["id"] == attempt_id


async def test_schedule_callback_rejects_past_time(test_db):
    past = _NOW - timedelta(hours=1)
    assert (
        await schedule_callback(
            org_id="org1", customer_id="c1", phone="+911234567890", at=past, now=_NOW
        )
        is None
    )


async def test_schedule_callback_requires_phone(test_db):
    at = _NOW + timedelta(hours=1)
    assert (
        await schedule_callback(org_id="org1", customer_id="c1", phone="", at=at, now=_NOW)
        is None
    )
