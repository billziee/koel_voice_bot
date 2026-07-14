"""In-call revenue actions: payment-link SMS + promise-to-pay (2.1)."""

from __future__ import annotations

from koel.common import messaging as msg
from koel.common.config import CommonSettings
from koel.common.messaging import Channel, send_message
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import SessionState
from tests.conftest import FakeClients

# --- messaging client: dormant without a gateway --------------------------

async def test_messaging_dormant_without_gateway():
    assert await send_message(Channel.sms, "+919000000000", "hi", settings=CommonSettings()) is False


async def test_messaging_no_recipient_is_dormant():
    s = CommonSettings(sms_gateway_url="https://sms.test/send", sms_api_key="k")
    assert await send_message(Channel.sms, "", "hi", settings=s) is False


class _FakeResp:
    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> _FakeResp:
        return _FakeResp()


async def test_messaging_sends_when_configured(monkeypatch):
    s = CommonSettings(sms_gateway_url="https://sms.test/send", sms_api_key="k")
    monkeypatch.setattr(msg, "make_async_client", lambda **kw: _FakeClient())
    assert await send_message(Channel.sms, "+919000000000", "pay now", settings=s) is True


# --- orchestrator action dispatch ------------------------------------------

def _orch(**settings_over) -> Orchestrator:
    return Orchestrator(
        settings=VoiceBotSettings(**settings_over), clients=FakeClients(), filler_cache=FillerCache()
    )


def _state() -> SessionState:
    st = SessionState(session_id="s", bot_id="b", customer_id="cust1", use_case="collections")
    st.crm_context = {"phone": "+919000000000"}
    return st


async def test_promise_to_pay_records_a_structured_ptp_on_the_crm():
    orch = _orch()
    await orch._dispatch_actions(
        _state(), [{"type": "promise_to_pay", "amount": 4500, "date": "2026-07-20"}]
    )
    assert ("cust1", "promise_to_pay", "amount=4500 date=2026-07-20") in orch._clients.flags


async def test_payment_link_records_intent_with_minted_link():
    orch = _orch(payment_link_base_url="https://pay.test")
    await orch._dispatch_actions(_state(), [{"type": "send_payment_link", "amount": 4500}])
    assert any(
        f[1] == "payment_link_sent" and "https://pay.test/pay?ref=cust1&amt=4500" in f[2]
        for f in orch._clients.flags
    )


async def test_payment_link_without_provider_still_records_intent():
    # No provider configured → no link minted, but the intent is still logged.
    orch = _orch()  # payment_link_base_url empty
    await orch._dispatch_actions(_state(), [{"type": "send_payment_link", "amount": 4500}])
    assert any(f[1] == "payment_link_sent" and "link=n/a" in f[2] for f in orch._clients.flags)


async def test_unknown_action_is_ignored_without_crashing():
    orch = _orch()
    await orch._dispatch_actions(_state(), [{"type": "launch_rockets"}])
    assert orch._clients.flags == []
