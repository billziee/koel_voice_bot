"""Operator console API for human handoff (3.1 Phase A).

When the bot escalates, the call parks in ``waiting_agent`` and a row lands in
the escalation queue carrying the full transcript + CRM context (PRD §02). A
logged-in operator lists the queue, claims a handoff, then drives the call by
text — each reply is spoken to the caller via TTS through the live bridge — and
resolves it when done. Tenant-scoped (AF): a scoped operator only sees/acts on
their own org's handoffs.

PSTN warm transfer (Phase B, 3.1): once a handoff is claimed, an operator with
a phone number can request a carrier-level warm transfer — the customer's
live call and a new outbound call to the operator are moved into the same
ad-hoc Twilio conference, so the operator ends up talking to the customer
directly instead of relaying text through the bot's TTS. Only applies to a
PSTN call (``SessionState.twilio_call_sid`` set); a browser call has no
underlying carrier leg to redirect. Purely additive — the existing text-relay
flow (reply/resolve) is unaffected and remains the fallback.

Out of scope for Phase A (tracked as a follow-up): live transcript streaming
to the operator (a second WS / supervisor monitor) — a browser operator
console UI now exists (frontend).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.common.logging import get_logger
from koel.common.ratelimit import rate_limit
from koel.db.models import EscalationStatus
from koel.voicebot.deps import Runtime, get_runtime
from koel.voicebot.persistence import (
    claim_escalation,
    close_escalation,
    get_escalation,
    list_pending_escalations,
)
from koel.voicebot.pipeline.warm_transfer import start_warm_transfer

logger = get_logger("koel.voicebot.operators")
router = APIRouter(prefix="/v1/operator", tags=["operator"])
_reply_limit = rate_limit(max_requests=120, window_s=60, name="operator")
# Phone-carrier calls are comparatively rare + operator-initiated (not a
# customer-facing hot path) — a tighter cap than _reply_limit is appropriate.
_warm_transfer_limit = rate_limit(max_requests=20, window_s=60, name="operator_warm_transfer")


class EscalationOut(BaseModel):
    id: str
    session_id: str
    bot_id: str
    customer_id: str | None = None
    reason: str
    language: str
    status: str
    operator_sub: str | None = None
    transcript: list = Field(default_factory=list)
    crm_context: dict = Field(default_factory=dict)
    created_at: str | None = None
    claimed_at: str | None = None
    resolved_at: str | None = None


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.get("/queue", response_model=list[EscalationOut])
async def queue(user: CurrentUser) -> list[dict]:
    """Pending handoffs waiting for an operator, oldest first (org-scoped)."""
    return await list_pending_escalations(user.org_id)


@router.post("/escalations/{escalation_id}/claim", response_model=EscalationOut)
async def claim(escalation_id: str, user: CurrentUser) -> dict:
    """Take ownership of a pending handoff. 404 if missing/cross-tenant; 409 if
    it was already claimed or is no longer pending."""
    row = await claim_escalation(escalation_id, user.org_id, user.sub)
    if row is None:
        # Distinguish "gone/not yours" (404) from "already taken" (409) without
        # leaking existence across tenants: re-read within the caller's org scope.
        existing = await get_escalation(escalation_id, user.org_id)
        if existing is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="escalation_not_found",
                message="Escalation not found.",
            )
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="escalation_not_pending",
            message="This handoff is no longer available to claim.",
        )
    logger.info(
        "escalation_claimed",
        extra={"extra_fields": {"escalation_id": escalation_id, "operator": user.sub}},
    )
    return row


@router.post(
    "/escalations/{escalation_id}/reply",
    response_model=EscalationOut,
    dependencies=[Depends(_reply_limit)],
)
async def reply(
    escalation_id: str,
    payload: ReplyRequest,
    user: CurrentUser,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    """Speak the operator's reply to the caller via TTS. Requires the handoff to
    be claimed; 410 if the caller has already dropped (no live socket)."""
    row = await get_escalation(escalation_id, user.org_id)
    if row is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="escalation_not_found",
            message="Escalation not found.",
        )
    if row["status"] != EscalationStatus.claimed.value:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="escalation_not_claimed",
            message="Claim the handoff before replying.",
        )
    if row.get("operator_sub") != user.sub:
        # Org + claimed is not enough: only the operator who CLAIMED it may speak on
        # the call — otherwise a second operator in the same org could talk over the
        # claimer's live handoff.
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="not_claim_owner",
            message="This handoff was claimed by another operator.",
        )
    delivered = await runtime.escalations.deliver(row["session_id"], payload.text)
    if not delivered:
        raise ApiError(
            status_code=status.HTTP_410_GONE,
            code="caller_disconnected",
            message="The caller is no longer connected.",
        )
    return row


class WarmTransferRequest(BaseModel):
    operator_phone: str = Field(min_length=6, max_length=20)


class WarmTransferOut(BaseModel):
    started: bool


@router.post(
    "/escalations/{escalation_id}/warm-transfer",
    response_model=WarmTransferOut,
    dependencies=[Depends(_warm_transfer_limit)],
)
async def warm_transfer(
    escalation_id: str,
    payload: WarmTransferRequest,
    user: CurrentUser,
    runtime: Runtime = Depends(get_runtime),
) -> WarmTransferOut:
    """Carrier-level PSTN handoff (3.1 Phase B): move the customer's live call
    and a new outbound call to the operator into the same conference. Same
    claim-ownership rules as ``reply``. 409 if the underlying call isn't a
    PSTN leg (browser call — nothing to redirect)."""
    row = await get_escalation(escalation_id, user.org_id)
    if row is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="escalation_not_found",
            message="Escalation not found.",
        )
    if row["status"] != EscalationStatus.claimed.value:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="escalation_not_claimed",
            message="Claim the handoff before requesting a warm transfer.",
        )
    if row.get("operator_sub") != user.sub:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="not_claim_owner",
            message="This handoff was claimed by another operator.",
        )
    state = await runtime.store.load(row["session_id"])
    if state is None or not state.twilio_call_sid:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="not_a_pstn_call",
            message="This call has no PSTN leg to transfer (browser call, or the caller is gone).",
        )
    started = await start_warm_transfer(
        call_sid=state.twilio_call_sid,
        operator_phone=payload.operator_phone,
        account_sid=runtime.settings.twilio_account_sid,
        auth_token=runtime.settings.twilio_auth_token,
        from_number=runtime.settings.twilio_from_number,
    )
    if not started:
        raise ApiError(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="warm_transfer_failed",
            message="Could not start the warm transfer. The text-relay flow is still available.",
        )
    logger.info(
        "warm_transfer_requested",
        extra={"extra_fields": {"escalation_id": escalation_id, "operator": user.sub}},
    )
    return WarmTransferOut(started=True)


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationOut)
async def resolve(
    escalation_id: str,
    user: CurrentUser,
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    """Finish the handoff: mark it resolved, then end the caller's call. Marking
    the DB first means the caller's finalize won't race it to 'abandoned'."""
    # Ownership: only the operator who claimed the handoff may resolve it. Check
    # before closing so another org-mate can't resolve someone else's live call.
    existing = await get_escalation(escalation_id, user.org_id)
    if existing is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="escalation_not_found",
            message="Escalation not found.",
        )
    if existing.get("operator_sub") != user.sub:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="not_claim_owner",
            message="This handoff was claimed by another operator.",
        )
    row = await close_escalation(escalation_id, user.org_id, EscalationStatus.resolved)
    if row is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="escalation_not_found",
            message="Escalation not found.",
        )
    # Best-effort: the caller may already be gone (then the row was resolved anyway).
    await runtime.escalations.resolve(row["session_id"])
    logger.info(
        "escalation_resolved",
        extra={"extra_fields": {"escalation_id": escalation_id, "operator": user.sub}},
    )
    return row


# --- Live supervisor monitor (3.3) ------------------------------------------


class MonitorCall(BaseModel):
    session_id: str
    bot_id: str
    status: str
    turn_count: int
    opened_at: str
    last_at: str


@router.get("/monitor", response_model=list[MonitorCall])
async def monitor_calls(
    user: CurrentUser, runtime: Runtime = Depends(get_runtime)
) -> list[dict]:
    """Read-only list of in-flight calls, org-scoped — every process/instance
    when the monitor is Redis-backed, this one only when it isn't."""
    return await runtime.monitor.list_active(user.org_id)


@router.get("/monitor/{session_id}")
async def monitor_call_detail(
    session_id: str, user: CurrentUser, runtime: Runtime = Depends(get_runtime)
) -> dict:
    """Live detail for one call: status + recent transcript. 404 if the call
    isn't live (ended, or — only when the monitor is in-process, not
    Redis-backed — served by a different instance)."""
    detail = await runtime.monitor.get(session_id, user.org_id)
    if detail is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="call_not_live",
            message="No live call with that id on this instance.",
        )
    return detail
