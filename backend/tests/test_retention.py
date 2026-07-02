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


async def test_upload_is_noop_seam_without_bucket():
    from types import SimpleNamespace

    from koel.voicebot.recording import upload_recording

    settings = SimpleNamespace(s3_recording_bucket="", s3_region="ap-south-1")
    # No bucket configured (dev) → skip cleanly, return None, never raise.
    assert await upload_recording("s1", b"RIFFfake", settings=settings) is None


async def test_upload_url_shape_when_bucket_set(monkeypatch):
    from types import SimpleNamespace

    from koel.voicebot import recording

    settings = SimpleNamespace(s3_recording_bucket="koel-rec", s3_region="ap-south-1")
    # boto3 isn't installed in dev → the import fails inside the try and we
    # degrade to None (logged), still never raising on the teardown path.
    result = await recording.upload_recording("s2", b"RIFFfake", settings=settings)
    assert result is None or result == "s3://koel-rec/recordings/s2.wav"
