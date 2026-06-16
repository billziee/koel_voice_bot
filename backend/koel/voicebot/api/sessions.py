"""Session initialisation (PRD §05 step 1 + §09 Screen 3 CRM Context Panel).

The frontend calls POST /v1/sessions to start a call: VB_Svc fetches the CRM
record, loads the bot config, seeds RAG context, creates the Redis session, and
returns the session_id + a short-lived ws_token to open the call socket.

TRAI DND scrub (PRD §11): a DND customer is refused here before any dial.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from koel.common.auth import CurrentUser, issue_token
from koel.common.errors import ApiError
from koel.common.schemas import Language, Tone
from koel.db.models import BotConfig
from koel.db.session import session_scope
from koel.voicebot.deps import Runtime, get_runtime
from koel.voicebot.session.models import SessionState

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class StartSessionRequest(BaseModel):
    bot_id: str
    customer_id: str | None = None


class StartSessionResponse(BaseModel):
    session_id: str
    ws_token: str
    ws_url: str
    crm_context: dict
    language: str
    use_case: str


@router.post("", response_model=StartSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_session(
    payload: StartSessionRequest,
    user: CurrentUser,
    runtime: Runtime = Depends(get_runtime),
) -> StartSessionResponse:
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
            **customer.attributes,
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

    state = SessionState(
        session_id=session_id,
        bot_id=bot.id,
        owner_sub=user.sub,
        customer_id=payload.customer_id,
        use_case=bot.use_case.value,
        language=language,
        rag_enabled=bot.rag_enabled,
        rag_index=bot.rag_index,
        default_tone=default_tone,
        ambient_profile=bot.ambient_profile,
        greeting_template=bot.greeting_template or "",
        crm_context=crm_context,
    )
    await runtime.store.save(state)

    return StartSessionResponse(
        session_id=session_id,
        ws_token=issue_token(user.sub, kind="ws", role=user.role),
        ws_url=f"/ws/calls/{session_id}",
        crm_context=crm_context,
        language=language.value,
        use_case=bot.use_case.value,
    )
