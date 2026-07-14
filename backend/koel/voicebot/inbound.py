"""Inbound call bootstrap (roadmap 3.2): resolve a caller by their number (ANI)
and build an INTENT-FIRST session.

KOEL is outbound-first: an authed HTTP call names the customer and the bot opens
with an outbound pitch. Inbound flips that — the caller dials in, we resolve them
by the calling number, and the session is marked so the conversation is reactive
(the bot asks how it can help) rather than launching the outbound script.

The live transport (``/twilio/stream``) already verifies the Twilio signature,
loads the default inbound bot, and reads the caller ``From`` number; it calls
:func:`build_inbound_session` at the point it currently seeds the session.

Threading "this session is inbound": :class:`SessionState` carries no direction
field (and is not editable here), so the direction is threaded through
``crm_context`` under the reserved ``"direction"`` key (value =
``CallDirection.incoming``). It flows session → ``TurnContext.crm_context`` →
NLP, where :func:`koel.nlp.providers.select_system_prompt` picks the intent-first
inbound system prompt. The greeting line itself stays driven by the inbound bot's
configured ``greeting_template`` (a deployment concern), so an inbound deployment
sets an intent-first greeting on its ``twilio_default_bot_id`` bot.
"""

from __future__ import annotations

from typing import Protocol

from koel.common.logging import get_logger, mask_account
from koel.common.schemas import Language
from koel.crm.schemas import CustomerRecord
from koel.db.models import BotConfig, CallDirection
from koel.voicebot.api.sessions import _minimise_attributes, build_session_state
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.inbound")

# Reserved crm_context key that threads call direction session → NLP (see module
# docstring). koel.nlp.providers keeps it out of the CRM prompt view.
DIRECTION_KEY = "direction"


class CrmLookup(Protocol):
    """The single CRM method the inbound bootstrap needs (structural typing so
    both :class:`~koel.voicebot.clients.ServiceClients` and test fakes satisfy it)."""

    async def get_customer_by_phone(
        self, phone: str, *, org_id: str | None = ...
    ) -> CustomerRecord | None: ...


def _coerce_language(value: str | None) -> Language | None:
    """Map a CRM language string onto :class:`Language`, or ``None`` when absent
    or unrecognised — ``None`` lets :func:`build_session_state` fall back to the
    bot's configured language."""
    if not value:
        return None
    try:
        return Language(value)
    except ValueError:
        return None


async def resolve_inbound_customer(
    clients: CrmLookup,
    phone: str | None,
    *,
    org_id: str | None = None,
) -> CustomerRecord | None:
    """Resolve the caller by their phone number (ANI lookup).

    Returns ``None`` when the phone is missing or unknown — an unknown caller is
    still served, as an anonymous inbound session. Never raises on a bad/unknown
    ANI: a lookup error degrades to ``None`` so a live inbound call is never
    dropped at bootstrap (consistent with the pipeline's best-effort posture).
    """
    if not phone:
        return None
    try:
        customer = await clients.get_customer_by_phone(phone, org_id=org_id)
    except Exception:  # noqa: BLE001 — never drop a live inbound call on a lookup error
        logger.warning(
            "inbound_ani_lookup_failed",
            extra={"extra_fields": {"phone": mask_account(phone)}},
            exc_info=True,
        )
        return None
    if customer is None:
        logger.info(
            "inbound_ani_unresolved",
            extra={"extra_fields": {"phone": mask_account(phone)}},
        )
    return customer


async def build_inbound_session(
    clients: CrmLookup,
    bot: BotConfig,
    *,
    caller_phone: str | None,
) -> SessionState:
    """Resolve the caller (if any) and build an intent-first inbound session.

    Reuses the shared :func:`build_session_state` bootstrap so per-bot
    backchannel / qa / followup / flow / experiment seeding never drifts from the
    outbound path. The session is marked inbound via ``crm_context["direction"]``
    (see the module docstring) so the NLP layer selects the intent-first prompt,
    and the caller's resolved language (else the bot default) drives TTS/NLP.
    """
    customer = await resolve_inbound_customer(clients, caller_phone, org_id=bot.org_id)

    crm_context: dict[str, object] = {DIRECTION_KEY: CallDirection.incoming.value}
    if caller_phone:
        crm_context = {**crm_context, "phone": caller_phone}

    customer_id: str | None = None
    language: Language | None = None
    if customer is not None:
        customer_id = customer.id
        # DPDP §11 data minimisation: only the use-case-relevant attributes, same
        # as the outbound HTTP path (shared helper — no drift).
        crm_context = {
            **crm_context,
            "name": customer.name,
            "bank": customer.bank,
            "phone": customer.phone,
            **_minimise_attributes(bot.use_case.value, customer.attributes),
        }
        language = _coerce_language(customer.language)

    logger.info(
        "inbound_session_bootstrapped",
        extra={
            "extra_fields": {
                "bot_id": bot.id,
                "resolved": customer is not None,
                "phone": mask_account(caller_phone),
            }
        },
    )
    return build_session_state(
        bot,
        customer_id=customer_id,
        crm_context=crm_context,
        language=language,
    )
