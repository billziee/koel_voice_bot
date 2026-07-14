"""Post-call WhatsApp follow-up (2.4)."""

from __future__ import annotations

from koel.voicebot.pipeline import followup as fu
from koel.voicebot.pipeline.followup import followup_body, send_followup
from koel.voicebot.session.models import SessionState


def test_followup_body_maps_outcome_to_template():
    templates = {"converted": "Thanks! Details on the way.", "declined": "No problem, take care."}
    assert followup_body("converted", templates) == "Thanks! Details on the way."
    assert followup_body("declined", templates) == "No problem, take care."


def test_followup_body_none_when_no_match_or_no_templates():
    assert followup_body("escalated", {"converted": "x"}) is None
    assert followup_body("converted", {}) is None
    assert followup_body("converted", None) is None


def _state(outcome: str, templates: dict, phone: str | None = "+919000000000") -> SessionState:
    s = SessionState(session_id="s", bot_id="b", customer_id="c", use_case="collections")
    s.outcome = outcome
    s.followup_templates = templates
    s.crm_context = {"phone": phone} if phone else {}
    return s


async def test_send_followup_dormant_without_matching_template():
    # No template for this outcome → no-op (regardless of channel config).
    assert await send_followup(_state("escalated", {"converted": "x"})) is False


async def test_send_followup_dormant_without_phone():
    assert await send_followup(_state("converted", {"converted": "hi"}, phone=None)) is False


async def test_send_followup_calls_messaging_when_template_and_phone_present(monkeypatch):
    sent: list[tuple] = []

    async def fake_send(channel, to, body, **kw):  # noqa: ANN001
        sent.append((channel.value, to, body))
        return True

    monkeypatch.setattr(fu, "send_message", fake_send)
    ok = await send_followup(_state("converted", {"converted": "Aapko details bhej di hain"}))
    assert ok is True
    assert sent == [("whatsapp", "+919000000000", "Aapko details bhej di hain")]
