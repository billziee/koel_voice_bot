"""Write the finished call to PostgreSQL (PRD §05 step 6 / §06 / §11 audit).

On call end VB_Svc persists the full transcript, outcome tag, and call metadata,
then the Redis session is flushed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from koel.common.logging import get_logger
from koel.db.models import CallLog, CallOutcome, EscalationRequest, EscalationStatus, TranscriptTurn
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


# --- Human-handoff queue (3.1) ----------------------------------------------


def _escalation_to_dict(row: EscalationRequest) -> dict:
    """Serialise a row to a plain dict (detached-safe for the API layer)."""
    return {
        "id": row.id,
        "org_id": row.org_id,
        "session_id": row.session_id,
        "bot_id": row.bot_id,
        "customer_id": row.customer_id,
        "reason": row.reason,
        "language": row.language,
        "status": row.status.value,
        "operator_sub": row.operator_sub,
        "transcript": row.transcript,
        "crm_context": row.crm_context,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


async def create_escalation(state: SessionState, *, reason: str) -> str:
    """Persist a pending handoff carrying the full context (PRD §02). Returns its id."""
    async with session_scope() as db:
        row = EscalationRequest(
            org_id=state.org_id,
            session_id=state.session_id,
            bot_id=state.bot_id,
            customer_id=state.customer_id,
            reason=(reason or "")[:128],
            language=state.language.value,
            status=EscalationStatus.pending,
            transcript=[t.model_dump(mode="json") for t in state.transcript],
            crm_context=state.crm_context,
        )
        db.add(row)
        await db.flush()
        return row.id


async def list_pending_escalations(org_id: str | None) -> list[dict]:
    """Queue view: pending handoffs, oldest first. Org-scoped when the caller is
    tenant-bound (AF); an unscoped/admin token sees all."""
    async with session_scope() as db:
        stmt = select(EscalationRequest).where(
            EscalationRequest.status == EscalationStatus.pending
        )
        if org_id is not None:
            stmt = stmt.where(EscalationRequest.org_id == org_id)
        stmt = stmt.order_by(EscalationRequest.created_at.asc())
        rows = (await db.execute(stmt)).scalars().all()
        return [_escalation_to_dict(r) for r in rows]


async def get_escalation(escalation_id: str, org_id: str | None) -> dict | None:
    async with session_scope() as db:
        row = await db.get(EscalationRequest, escalation_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        return _escalation_to_dict(row)


async def claim_escalation(escalation_id: str, org_id: str | None, operator_sub: str) -> dict | None:
    """Atomically move pending→claimed and stamp the operator. Returns the row,
    or None if it's gone, cross-tenant, or already claimed by someone else."""
    async with session_scope() as db:
        row = await db.get(EscalationRequest, escalation_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        if row.status != EscalationStatus.pending:
            return None
        row.status = EscalationStatus.claimed
        row.operator_sub = operator_sub
        row.claimed_at = datetime.now(UTC)
        await db.flush()
        return _escalation_to_dict(row)


async def close_escalation(
    escalation_id: str, org_id: str | None, status: EscalationStatus
) -> dict | None:
    """Terminal transition (resolved/abandoned). Idempotent: a row already in a
    terminal state is returned unchanged."""
    async with session_scope() as db:
        row = await db.get(EscalationRequest, escalation_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        if row.status not in (EscalationStatus.resolved, EscalationStatus.abandoned):
            row.status = status
            row.resolved_at = datetime.now(UTC)
            await db.flush()
        return _escalation_to_dict(row)


async def abandon_escalation_by_session(session_id: str) -> None:
    """Caller dropped / grace expired: mark any still-open handoff abandoned.
    Best-effort; keyed by session so the handler doesn't need to track the id."""
    try:
        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(EscalationRequest).where(
                        EscalationRequest.session_id == session_id,
                        EscalationRequest.status.in_(
                            [EscalationStatus.pending, EscalationStatus.claimed]
                        ),
                    )
                )
            ).scalars().all()
            for row in rows:
                row.status = EscalationStatus.abandoned
                row.resolved_at = datetime.now(UTC)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("escalation_abandon_failed session_id=%s", session_id, exc_info=True)
