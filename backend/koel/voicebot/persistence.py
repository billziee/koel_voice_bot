"""Write the finished call to PostgreSQL (PRD §05 step 6 / §06 / §11 audit).

On call end VB_Svc persists the full transcript, outcome tag, and call metadata,
then the Redis session is flushed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from koel.common.logging import get_logger
from koel.db.models import CallLog, CallOutcome, TranscriptTurn
from koel.db.session import session_scope
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.persistence")


def _coerce_outcome(value: str) -> CallOutcome:
    try:
        return CallOutcome(value)
    except ValueError:
        return CallOutcome.in_progress


async def persist_call(state: SessionState) -> str | None:
    """Persist the call + transcript. Returns the call_log id, or None on failure."""
    now = datetime.now(UTC)
    duration_ms = int((now - state.started_at).total_seconds() * 1000)

    try:
        async with session_scope() as db:
            call = CallLog(
                session_id=state.session_id,
                bot_id=state.bot_id,
                customer_id=state.customer_id,
                language=state.language.value,
                outcome=_coerce_outcome(state.outcome),
                started_at=state.started_at,
                ended_at=now,
                duration_ms=duration_ms,
                escalated=state.escalated,
                recording_url=state.recording_url,
                # §11 audit + §14 latency dashboards: consent log + trace id.
                metrics={
                    "turns": len(state.transcript),
                    "consent_captured": state.consent_captured,
                    "consent_at": state.consent_at.isoformat() if state.consent_at else None,
                    "trace_id": state.trace_id,
                },
            )
            db.add(call)
            await db.flush()  # assign call.id for the FK below
            for turn in state.transcript:
                db.add(
                    TranscriptTurn(
                        call_id=call.id,
                        turn_index=turn.index,
                        speaker=turn.speaker,
                        text=turn.text,
                        language=turn.language.value if turn.language else None,
                        intent=turn.intent,
                        tone=turn.tone.value if turn.tone else None,
                        interrupted=turn.interrupted,
                        confidence=turn.confidence,
                    )
                )
        logger.info(
            "call_persisted",
            extra={"extra_fields": {"session_id": state.session_id, "outcome": state.outcome}},
        )
        return call.id
    except Exception:  # noqa: BLE001 — persistence failure must not crash the socket
        logger.error("call_persist_failed session_id=%s", state.session_id, exc_info=True)
        return None
