"""Call history + transcript replay (PRD §09 Screen 4).

Past calls listed with metadata; transcript expandable inline. Filterable by
bot, outcome, and language.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.db.models import CallLog, TranscriptTurn
from koel.db.session import session_scope

router = APIRouter(prefix="/v1/calls", tags=["history"])


class CallSummary(BaseModel):
    id: str
    session_id: str
    bot_id: str
    customer_id: str | None
    language: str
    outcome: str
    escalated: bool
    started_at: datetime
    duration_ms: int | None


class TurnOut(BaseModel):
    turn_index: int
    speaker: str
    text: str
    intent: str | None
    tone: str | None
    interrupted: bool


class CallDetail(CallSummary):
    transcript: list[TurnOut]


@router.get("", response_model=list[CallSummary])
async def list_calls(
    user: CurrentUser,
    bot_id: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    language: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> list[CallSummary]:
    stmt = select(CallLog).order_by(CallLog.started_at.desc()).limit(limit)
    if user.org_id is not None:  # tenant isolation (AF)
        stmt = stmt.where(CallLog.org_id == user.org_id)
    if bot_id:
        stmt = stmt.where(CallLog.bot_id == bot_id)
    if language:
        stmt = stmt.where(CallLog.language == language)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    summaries = [
        CallSummary(
            id=r.id,
            session_id=r.session_id,
            bot_id=r.bot_id,
            customer_id=r.customer_id,
            language=r.language,
            outcome=r.outcome.value,
            escalated=r.escalated,
            started_at=r.started_at,
            duration_ms=r.duration_ms,
        )
        for r in rows
    ]
    # Enum filter applied in Python so an invalid value yields [] not a 500.
    if outcome:
        summaries = [s for s in summaries if s.outcome == outcome]
    return summaries


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(call_id: str, user: CurrentUser) -> CallDetail:
    async with session_scope() as db:
        call = await db.get(CallLog, call_id)
        # Tenant isolation (AF): a scoped operator cannot read another org's call
        # transcript — 404 (not 403) so we don't leak that the call exists.
        if call is None or (user.org_id is not None and call.org_id != user.org_id):
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="call_not_found",
                message=f"No call with id {call_id}.",
            )
        turns = (
            await db.execute(
                select(TranscriptTurn)
                .where(TranscriptTurn.call_id == call_id)
                .order_by(TranscriptTurn.turn_index)
            )
        ).scalars().all()
    return CallDetail(
        id=call.id,
        session_id=call.session_id,
        bot_id=call.bot_id,
        customer_id=call.customer_id,
        language=call.language,
        outcome=call.outcome.value,
        escalated=call.escalated,
        started_at=call.started_at,
        duration_ms=call.duration_ms,
        transcript=[
            TurnOut(
                turn_index=t.turn_index,
                speaker=t.speaker,
                text=t.text,
                intent=t.intent,
                tone=t.tone,
                interrupted=t.interrupted,
            )
            for t in turns
        ],
    )
