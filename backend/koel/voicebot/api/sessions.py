"""Session initialisation (PRD §05 step 1 + §09 Screen 3 CRM Context Panel).

The frontend calls POST /v1/sessions to start a call: VB_Svc fetches the CRM
record, loads the bot config, seeds RAG context, creates the Redis session, and
returns the session_id + a short-lived ws_token to open the call socket.

TRAI DND scrub (PRD §11): a DND customer is refused here before any dial.
"""

from __future__ import annotations

import uuid

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
from koel.voicebot.pipeline.compliance import is_within_calling_hours
from koel.voicebot.pipeline.experiment import assign_variant
from koel.voicebot.pipeline.flow import parse_flow
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.sessions")
router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
_session_limit = rate_limit(max_requests=20, window_s=60, name="session")  # M: cap session spam per IP

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


def build_session_state(
    bot: BotConfig,
    *,
    customer_id: str | None,
    owner_sub: str | None = None,
    language: Language | None = None,
    crm_context: dict | None = None,
) -> SessionState:
    """Seed a fully-populated :class:`SessionState` from a bot instance's config.

    Shared by the authed HTTP bootstrap (``POST /v1/sessions``) and the inbound
    telephony route (``/twilio/stream``) so the seeding of language/tone, per-bot
    backchannels / qa_rules / followup_templates / flow, and the A/B experiment
    variant overrides lives in exactly ONE place (no drift between the two
    entry points). Pure: it builds and returns the state; the caller persists it
    (and, on the HTTP path, has already run the CRM/DND/calling-hours checks and
    supplies the minimised ``crm_context`` + ``owner_sub``).

    ``language`` defaults to the bot's configured language; pass it to override
    (e.g. a future per-caller language). ``customer_id`` also keys the
    deterministic A/B variant assignment, so the same customer lands in the same
    arm across retries.
    """
    session_id = uuid.uuid4().hex
    if language is None:
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
    # Per-bot script-adherence QA rules (5.5), scored deterministically at call end.
    qa_rules = bot.config.get("qa_rules", {}) if isinstance(bot.config, dict) else {}
    # Per-bot post-call follow-up templates (2.4): outcome → WhatsApp message.
    followup_templates = bot.config.get("followup_templates", {}) if isinstance(bot.config, dict) else {}
    # Per-bot scripted conversation flow (6.6): raw {start, nodes} dict + entry node.
    flow_config = bot.config.get("flow") if isinstance(bot.config, dict) else None
    _flow = parse_flow({"flow": flow_config})
    # Per-bot STT transport (O-6): "ws" opts into the streaming STT endpoint;
    # anything else (absent / invalid) stays on today's "chunk" batch path.
    # Seeded here — the one shared bootstrap — so the HTTP and telephony/inbound
    # paths never drift.
    raw_stt_mode = bot.config.get("stt_mode") if isinstance(bot.config, dict) else None
    stt_mode = raw_stt_mode if raw_stt_mode in ("chunk", "ws") else "chunk"
    # Per-bot BYOK model connections (LLM/TTS/STT). Each id, when set, routes that
    # service to a custom provider (resolved + decrypted server-side); absent ⇒
    # the service default. Ignore non-string entries defensively.
    _conns = bot.config.get("connections") if isinstance(bot.config, dict) else None
    _conns = _conns if isinstance(_conns, dict) else {}

    def _conn(role: str) -> str | None:
        v = _conns.get(role)
        return v if isinstance(v, str) and v else None

    # A/B experiment arm (6.3): deterministically assigned, overrides applied as
    # variant → bot → default. Same customer → same arm across retries.
    variant_id, overrides = assign_variant(bot.config, customer_id)
    greeting_template = str(overrides.get("greeting_template", bot.greeting_template or ""))
    prompt_preamble = overrides.get("prompt_preamble", bot.prompt_preamble)
    voice = overrides.get("voice", bot.config.get("voice") if isinstance(bot.config, dict) else None)
    speaking_rate = overrides.get("speaking_rate", bot.speaking_rate)
    if "default_tone" in overrides:
        try:
            default_tone = Tone(str(overrides["default_tone"]))
        except ValueError:
            pass  # keep the bot default on a bad variant value

    return SessionState(
        session_id=session_id,
        bot_id=bot.id,
        owner_sub=owner_sub,
        customer_id=customer_id,
        use_case=bot.use_case.value,
        org_id=bot.org_id,
        language=language,
        rag_enabled=bot.rag_enabled,
        rag_index=bot.rag_index,
        rag_top_k=bot.rag_top_k,
        rag_score_threshold=bot.rag_score_threshold,
        llm_model=bot.llm_model,
        llm_temperature=bot.llm_temperature,
        prompt_preamble=prompt_preamble,
        default_tone=default_tone,
        stt_mode=stt_mode,
        llm_connection_id=_conn("llm"),
        tts_connection_id=_conn("tts"),
        stt_connection_id=_conn("stt"),
        voice_gender=bot.voice_gender,
        voice=voice,
        speaking_rate=speaking_rate,
        ambient_profile=bot.ambient_profile,
        greeting_template=greeting_template,
        variant_id=variant_id,
        consent_disclosure_version=(
            bot.config.get("consent_disclosure_version") if isinstance(bot.config, dict) else None
        ),
        crm_context=crm_context or {},
        backchannels=backchannels if isinstance(backchannels, dict) else {},
        qa_rules=qa_rules if isinstance(qa_rules, dict) else {},
        followup_templates=followup_templates if isinstance(followup_templates, dict) else {},
        flow=flow_config if isinstance(flow_config, dict) else None,
        flow_node_id=_flow.start if _flow else None,
        # recording_url is populated on call end with the REAL S3 object (or left
        # NULL when recording is disabled) — never a phantom URL (audit finding).
        recording_url=None,
    )


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
    # RBI fair-practice calling-hours window (PRD §11) — shared with the
    # campaign dialer worker (pipeline/compliance.py) so both origination
    # paths enforce the identical window, not two copies that can drift.
    if not is_within_calling_hours(
        enforce=runtime.settings.enforce_calling_hours,
        tz_name=runtime.settings.calling_hours_tz,
        start_hour=runtime.settings.calling_hours_start,
        end_hour=runtime.settings.calling_hours_end,
    ):
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="outside_calling_hours",
            message="Calls are only permitted within RBI fair-practice hours.",
        )

    async with session_scope() as db:
        bot: BotConfig | None = await db.get(BotConfig, payload.bot_id)
    # Tenant isolation (AF): a scoped operator cannot start a session on another
    # org's bot (would hijack its config/RAG). 404 so the bot's existence isn't leaked.
    if bot is None or not bot.active or (user.org_id is not None and bot.org_id != user.org_id):
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="bot_not_found",
            message="Bot instance not found or inactive.",
        )

    crm_context: dict = {}
    if payload.customer_id:
        # Org-scoped fetch (AF): a scoped operator must not load another org's
        # customer PII via a foreign customer_id. Unknown/cross-tenant → clean 404.
        customer = await runtime.clients.get_customer(payload.customer_id, org_id=user.org_id)
        if customer is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message="Customer not found.",
            )
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

    # Seed the full call state via the shared bootstrap (reused by /twilio/stream)
    # so backchannel/qa/followup/flow/variant seeding never drifts between paths.
    state = build_session_state(
        bot,
        customer_id=payload.customer_id,
        owner_sub=user.sub,
        crm_context=crm_context,
    )
    await runtime.store.save(state)

    # Business event with PII masked (PRD §08/§14): name initials + masked id.
    logger.info(
        "session_started",
        extra={
            "extra_fields": {
                "session_id": state.session_id,
                "bot_id": bot.id,
                "customer": mask_account(payload.customer_id),
                "customer_name": mask_name(crm_context.get("name")),
                "use_case": bot.use_case.value,
            }
        },
    )

    return StartSessionResponse(
        session_id=state.session_id,
        ws_token=issue_token(user.sub, kind="ws", role=user.role, org_id=user.org_id),
        ws_url=f"/ws/calls/{state.session_id}",
        crm_context=crm_context,
        language=state.language.value,
        use_case=bot.use_case.value,
    )
