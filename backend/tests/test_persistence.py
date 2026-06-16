"""Integration test for persist_call → DB (PRD §05 step 6 / §11 audit)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from koel.common.schemas import Language, Tone
from koel.db.models import CallLog, CallOutcome, TranscriptTurn
from koel.voicebot.persistence import persist_call
from koel.voicebot.session.models import SessionState, Turn


def _state() -> SessionState:
    s = SessionState(session_id="sess_1", bot_id="bot_test", customer_id="cust_x", use_case="credit_card")
    s.language = Language.hinglish
    s.outcome = "call_dropped"
    s.escalated = False
    s.consent_captured = True
    s.consent_at = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    s.trace_id = "e94f8b1fbbe6f2a7b6f586bb58373578"
    s.recording_url = "s3://koel-recordings/sess_1.wav"
    s.add_turn(Turn(index=0, speaker="bot", text="Namaste", tone=Tone.cheerful, intent="greeting"))
    s.add_turn(Turn(index=1, speaker="customer", text="Haan boliye", confidence=0.9))
    return s


async def test_persist_call_writes_call_and_transcript(test_db):
    call_id = await persist_call(_state())
    assert call_id is not None

    async with test_db() as db:
        call = (await db.execute(select(CallLog).where(CallLog.session_id == "sess_1"))).scalar_one()
        turns = (
            await db.execute(select(TranscriptTurn).where(TranscriptTurn.call_id == call_id))
        ).scalars().all()

    # Outcome coerces onto the enum; audit fields persisted.
    assert call.outcome == CallOutcome.call_dropped
    assert call.recording_url == "s3://koel-recordings/sess_1.wav"
    assert call.metrics["trace_id"] == "e94f8b1fbbe6f2a7b6f586bb58373578"
    assert call.metrics["consent_at"] == "2026-06-16T12:00:00+00:00"
    assert call.metrics["consent_captured"] is True
    assert call.duration_ms is not None
    # Both turns persisted with their tags.
    assert len(turns) == 2
    bot_turn = next(t for t in turns if t.speaker == "bot")
    assert bot_turn.tone == "cheerful" and bot_turn.intent == "greeting"


async def test_persist_call_coerces_unknown_outcome(test_db):
    s = _state()
    s.session_id = "sess_2"
    s.outcome = "not_a_real_outcome"
    call_id = await persist_call(s)
    assert call_id is not None
    async with test_db() as db:
        call = (await db.execute(select(CallLog).where(CallLog.session_id == "sess_2"))).scalar_one()
    assert call.outcome == CallOutcome.in_progress  # graceful coercion fallback
