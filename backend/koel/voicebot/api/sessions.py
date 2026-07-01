"""Session initialisation (PRD §05 step 1 + §09 Screen 3 CRM Context Panel).

The frontend calls POST /v1/sessions to start a call: VB_Svc fetches the CRM
record, loads the bot config, seeds RAG context, creates the Redis session, and
returns the session_id + a short-lived ws_token to open the call socket.

TRAI DND scrub (PRD §11): a DND customer is refused here before any dial.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from koel.common.auth import CurrentUser, issue_token
from koel.common.errors import ApiError
from koel.common.logging import get_logger, mask_account, mask_name
from koel.common.ratelimit import rate_limit
from koel.common.schemas import Language, Tone
from koel.db.models import BotConfig
from koel.db.session import session_scope
from koel.voicebot.deps import Runtime, get_runtime
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.sessions")
router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
_session_limit = rate_limit(max_requests=20, window_s=60)  # M: cap session spam per IP

# DPDP Act §11 data minimisation: load only the CRM attributes each use case
# actually needs into the session. Use cases not listed here load all attributes.
_USE_CASE_ATTRS: dict[str, set[str]] = {
    "credit_card": {
        "product", "offer_type", "annual_fee_waived", "existing_cards",
        "bureau_score", "benefits",
    },
    "collections": {"outstanding_amount", "due_date", "dpd", "segment", "min_payment"},
    "personal_loan": {"pre_approved_amount", "tenure_months", "indicative_emi", "interest_rate"},
    "insurance": {"product", "sum_assured", "annual_premium", "nominee_required", "irdai_disclosures"},
}


def _minimise_attributes(use_case: str, attributes: dict) -> dict:
    """Keep only use-case-relevant CRM fields (DPDP purpose limitation, §11)."""
    allowed = _USE_CASE_ATTRS.get(use_case)
    if allowed is None:
        return dict(attributes)
    return {k: v for k, v in attributes.items() if k in allowed}


class StartSessionRequest(BaseModel):
    bot_id: str = Field(max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)


class StartSessionResponse(BaseModel):
    session_id: str
    ws_token: str
    ws_url: str
    crm_context: dict
    language: str
    use_case: str


@router.post(
    "",
    response_model=StartSessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_session_limit)],
)
async def start_session(
    payload: StartSessionRequest,
    user: CurrentUser,
    runtime: Runtime = Depends(get_runtime),
) -> StartSessionResponse:
    # RBI fair-practice calling-hours window (PRD §11). Opt-in via config so
    # demos run any time; when enabled, out-of-hours dials are refused.
    if runtime.settings.enforce_calling_hours:
        hour = datetime.now().hour
        if not (runtime.settings.calling_hours_start <= hour < runtime.settings.calling_hours_end):
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="outside_calling_hours",
                message="Calls are only permitted within RBI fair-practice hours.",
            )

    async with session_scope() as db:
        bot: BotConfig | None = await db.get(BotConfig, payload.bot_id)
    if bot is None or not bot.active:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="bot_not_found",
            message="Bot instance not found or inactive.",
        )

    crm_context: dict = {}
    if payload.customer_id:
        customer = await runtime.clients.get_customer(payload.customer_id)
        if customer.dnd:
            # TRAI DND scrub before dialling (PRD §11).
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code="dnd_registered",
                message="Customer is on the DND registry; call refused.",
            )
        crm_context = {
            "name": customer.name,
            "bank": customer.bank,
            "phone": customer.phone,
            # DPDP §11: only load attributes this use case needs.
            **_minimise_attributes(bot.use_case.value, customer.attributes),
        }

    session_id = uuid.uuid4().hex
    try:
        language = Language(bot.language)
    except ValueError:
        language = Language.hinglish
    try:
        default_tone = Tone(bot.default_tone)
    except ValueError:
        default_tone = Tone.neutral

    # Per-bot backchannel overrides for Gate 2 (PRD §05), if configured.
    backchannels = bot.config.get("backchannels", {}) if isinstance(bot.config, dict) else {}

    state = SessionState(
        session_id=session_id,
        bot_id=bot.id,
        owner_sub=user.sub,
        customer_id=payload.customer_id,
        use_case=bot.use_case.value,
        org_id=bot.org_id,
        language=language,
        rag_enabled=bot.rag_enabled,
        rag_index=bot.rag_index,
        rag_top_k=bot.rag_top_k,
        rag_score_threshold=bot.rag_score_threshold,
        default_tone=default_tone,
        voice_gender=bot.voice_gender,
        speaking_rate=bot.speaking_rate,
        ambient_profile=bot.ambient_profile,
        greeting_template=bot.greeting_template or "",
        crm_context=crm_context,
        backchannels=backchannels if isinstance(backchannels, dict) else {},
        # Intended recording object for the §11/§14 audit trail (30-day S3).
        recording_url=f"s3://koel-recordings/{session_id}.wav",
    )
    await runtime.store.save(state)

    # Business event with PII masked (PRD §08/§14): name initials + masked id.
    logger.info(
        "session_started",
        extra={
            "extra_fields": {
                "session_id": session_id,
                "bot_id": bot.id,
                "customer": mask_account(payload.customer_id),
                "customer_name": mask_name(crm_context.get("name")),
                "use_case": bot.use_case.value,
            }
        },
    )

    return StartSessionResponse(
        session_id=session_id,
        ws_token=issue_token(user.sub, kind="ws", role=user.role, org_id=user.org_id),
        ws_url=f"/ws/calls/{session_id}",
        crm_context=crm_context,
        language=language.value,
        use_case=bot.use_case.value,
    )
