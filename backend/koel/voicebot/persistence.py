"""Write the finished call to PostgreSQL (PRD §05 step 6 / §06 / §11 audit).

On call end VB_Svc persists the full transcript, outcome tag, and call metadata,
then the Redis session is flushed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from koel.common.logging import get_logger
from koel.db.models import CallLog, CallOutcome, TranscriptTurn
from koel.db.session import session_scope
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.persistence")


def _coerce_outcome(value: str) -> CallOutcome:
    try:
        outcome = CallOutcome(value)
    except ValueError:
        outcome = CallOutcome.in_progress
    # A persisted call has, by definition, ended — never leave it "in_progress"
    # or it pollutes the AE conversion/aggregation denominators.
    if outcome is CallOutcome.in_progress:
        return CallOutcome.customer_unresponsive
    return outcome


async def persist_call(state: SessionState) -> str | None:
    """Persist the call + transcript. Returns the call_log id, or None on failure."""
    now = datetime.now(UTC)
    duration_ms = int((now - state.started_at).total_seconds() * 1000)

    try:
        async with session_scope() as db:
            call = CallLog(
                session_id=state.session_id,
                bot_id=state.bot_id,
                org_id=state.org_id,  # tenant scope for AF filtering
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


async def persist_call_summary(session_id: str, summary: dict) -> None:
    """Merge the post-call LLM summary (interest/objection/sentiment/next_action)
    into the call's ``metrics`` so operators see it in the history API (roadmap
    5.3). Runs off the critical path after the row is persisted; best-effort —
    a failure is logged, never raised.
    """
    try:
        async with session_scope() as db:
            call = (
                await db.execute(select(CallLog).where(CallLog.session_id == session_id))
            ).scalars().first()
            if call is None:
                return
            # SQLAlchemy only tracks a JSON column change on reassignment, so build
            # a new dict rather than mutating call.metrics in place.
            call.metrics = {**(call.metrics or {}), "summary": summary}
    except Exception:  # noqa: BLE001 — best-effort enrichment, never raise
        logger.warning("call_summary_persist_failed session_id=%s", session_id, exc_info=True)
