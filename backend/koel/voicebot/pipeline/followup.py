"""Post-call WhatsApp/SMS follow-up (2.4).

After a call ends, send an outcome-specific message (offer summary, payment link,
callback confirmation) over WhatsApp — open rates dwarf SMS in India. Templates
are per bot in ``BotConfig.config['followup_templates']`` (outcome → text), seeded
onto the session. Off the critical path (fired after persist) and DORMANT unless
both a template matches and the WhatsApp gateway is configured, so it degrades
cleanly with empty keys.
"""

from __future__ import annotations

from koel.common.logging import get_logger
from koel.common.messaging import Channel, send_message
from koel.voicebot.session.models import SessionState

logger = get_logger("koel.voicebot.followup")


def followup_body(outcome: str, templates: dict | None) -> str | None:
    """The follow-up text for this call's outcome, or None if none is configured."""
    if not templates:
        return None
    body = templates.get(outcome)
    return str(body) if body else None


async def send_followup(state: SessionState) -> bool:
    """Send the outcome's follow-up over WhatsApp. Returns False (no-op) when
    there's no matching template, no customer phone, or the channel is dormant."""
    body = followup_body(state.outcome, state.followup_templates)
    phone = state.crm_context.get("phone") if isinstance(state.crm_context, dict) else None
    if not body or not phone:
        return False
    return await send_message(
        Channel.whatsapp, str(phone), body, idempotency_key=f"{state.session_id}:followup"
    )
