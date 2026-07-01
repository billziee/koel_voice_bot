"""Retention cleanup + recording seam (K)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from koel.db.models import CallLog, CallOutcome
from scripts.retention_cleanup import cleanup


async def _add_call(maker, call_id, started_at):
    async with maker() as s:
        s.add(CallLog(
            id=call_id, session_id=call_id, bot_id="b", language="hinglish",
            outcome=CallOutcome.converted, started_at=started_at,
        ))
        await s.commit()


async def _count(maker) -> int:
    async with maker() as s:
        return (await s.execute(select(func.count()).select_from(CallLog))).scalar_one()


async def test_cleanup_deletes_only_stale_rows(test_db):
    now = datetime.now(UTC)
    await _add_call(test_db, "old", now - timedelta(days=45))
    await _add_call(test_db, "fresh", now - timedelta(days=2))

    deleted = await cleanup(days=30)
    assert deleted == 1
    assert await _count(test_db) == 1  # the fresh one survives


async def test_cleanup_dry_run_deletes_nothing(test_db):
    now = datetime.now(UTC)
    await _add_call(test_db, "old", now - timedelta(days=90))
    reported = await cleanup(days=30, dry_run=True)
    assert reported == 1
    assert await _count(test_db) == 1  # dry-run left it in place


async def test_recording_key_matches_persisted_url():
    from koel.voicebot.recording import recording_key

    assert recording_key("SES-ABC") == "recordings/SES-ABC.wav"
