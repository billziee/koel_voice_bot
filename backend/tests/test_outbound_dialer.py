"""Outbound call origination (1.2) — the dialer worker + Twilio Calls API client."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from koel.voicebot.campaigns import claim_due_attempts, enqueue_dial, get_attempt, record_dial_result
from koel.voicebot.pipeline.compliance import is_within_calling_hours
from koel.voicebot.pipeline.dialer_worker import _bot_id_for, _dnd_scrubbed, dialer_enabled
from koel.voicebot.pipeline.outbound_dialer import (
    call_outcome_to_dial_outcome,
    call_status_to_dial_outcome,
    originate_call,
)

# --- compliance --------------------------------------------------------------


def test_calling_hours_disabled_always_allows():
    assert is_within_calling_hours(enforce=False, tz_name="Asia/Kolkata", start_hour=9, end_hour=21)


def test_calling_hours_enforced_window():
    tz_name = "UTC"
    inside = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    outside = datetime(2026, 7, 3, 23, 0, tzinfo=UTC)
    assert is_within_calling_hours(
        enforce=True, tz_name=tz_name, start_hour=9, end_hour=21, now=inside
    )
    assert not is_within_calling_hours(
        enforce=True, tz_name=tz_name, start_hour=9, end_hour=21, now=outside
    )


def test_calling_hours_bad_timezone_fails_safe_to_utc():
    now = datetime(2026, 7, 3, 10, 0, tzinfo=UTC)
    assert is_within_calling_hours(
        enforce=True, tz_name="Not/A_Real_Zone", start_hour=9, end_hour=21, now=now
    )


# --- outcome vocabulary translation ------------------------------------------


def test_call_outcome_maps_known_values():
    assert call_outcome_to_dial_outcome("converted") == "completed"
    assert call_outcome_to_dial_outcome("no_answer") == "no_answer"
    assert call_outcome_to_dial_outcome("call_dropped") == "network_error"


def test_call_outcome_unmapped_passes_through():
    # Dialer treats an unrecognised outcome as non-retryable (fail closed) —
    # confirming the translation doesn't invent a mapping for something new.
    assert call_outcome_to_dial_outcome("some_future_outcome") == "some_future_outcome"


def test_call_status_maps_known_values():
    assert call_status_to_dial_outcome("no-answer") == "no_answer"
    assert call_status_to_dial_outcome("busy") == "busy"
    assert call_status_to_dial_outcome("failed") == "failed"


def test_call_status_in_progress_is_not_a_final_outcome():
    assert call_status_to_dial_outcome("in-progress") is None
    assert call_status_to_dial_outcome("completed") is None  # bridge teardown owns this one


# --- dialer_enabled dormancy gate --------------------------------------------


class _Settings:
    def __init__(self, **kw):
        self.twilio_account_sid = kw.get("twilio_account_sid", "")
        self.twilio_auth_token = kw.get("twilio_auth_token", "")
        self.twilio_from_number = kw.get("twilio_from_number", "")
        self.public_base_url = kw.get("public_base_url", "")


def test_dialer_disabled_when_any_config_missing():
    assert not dialer_enabled(_Settings())
    assert not dialer_enabled(_Settings(twilio_account_sid="AC1"))
    assert not dialer_enabled(
        _Settings(
            twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+1"
        )
    )


def test_dialer_enabled_when_fully_configured():
    assert dialer_enabled(
        _Settings(
            twilio_account_sid="AC1",
            twilio_auth_token="tok",
            twilio_from_number="+1",
            public_base_url="https://example.com",
        )
    )


# --- originate_call -----------------------------------------------------------


async def test_originate_call_skips_when_unconfigured():
    attempt = {"id": "a1", "phone": "+911234567890"}
    sid = await originate_call(
        attempt,
        bot_id="bot1",
        account_sid="",
        auth_token="",
        from_number="",
        public_base_url="",
    )
    assert sid is None


async def test_originate_call_success_returns_sid(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+911234567890"
    )
    await claim_due_attempts(limit=10)
    attempt = await get_attempt(attempt_id)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"sid": "CA123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sid = await originate_call(
        attempt,
        bot_id="bot1",
        account_sid="AC1",
        auth_token="tok",
        from_number="+10000000000",
        public_base_url="https://example.com",
        client=client,
    )
    await client.aclose()

    from urllib.parse import parse_qs

    assert sid == "CA123"
    fields = parse_qs(captured["body"])
    assert fields["To"] == ["+911234567890"]
    assert fields["From"] == ["+10000000000"]
    assert fields["StatusCallbackEvent"] == ["completed", "no-answer", "busy", "failed"]
    assert "wss://example.com/twilio/stream" in fields["Twiml"][0]
    assert f"attempt_id={attempt_id}" in fields["Twiml"][0]


async def test_originate_call_failure_records_network_error(test_db):
    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+911234567890"
    )
    await claim_due_attempts(limit=10)
    attempt = await get_attempt(attempt_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sid = await originate_call(
        attempt,
        bot_id="bot1",
        account_sid="AC1",
        auth_token="tok",
        from_number="+10000000000",
        public_base_url="https://example.com",
        client=client,
    )
    await client.aclose()

    assert sid is None
    updated = await record_dial_result(attempt_id, "no_answer")
    # Already moved off in_progress by the network_error path (record_dial_result
    # inside originate_call's failure branch already reset it to pending/failed),
    # so this second call is a no-op — confirms the failure path really did call
    # record_dial_result rather than leaving the row stuck in_progress forever.
    assert updated is None


# --- dialer_worker helpers ----------------------------------------------------


async def test_bot_id_for_campaignless_attempt_uses_default():
    assert await _bot_id_for(None, "default-bot") == "default-bot"
    assert await _bot_id_for(None, "") is None


async def test_bot_id_for_unknown_campaign_is_none(test_db):
    assert await _bot_id_for("no-such-campaign", "default-bot") is None


class _FakeCrm:
    def __init__(self, dnd: bool):
        self._dnd = dnd

    async def get_customer(self, customer_id, *, org_id=None):
        class _Customer:
            dnd = self._dnd

        return _Customer()


class _RaisingCrm:
    async def get_customer(self, customer_id, *, org_id=None):
        raise RuntimeError("CRM unreachable")


async def test_dnd_scrubbed_true_for_dnd_customer():
    attempt = {"id": "a1", "customer_id": "c1", "org_id": "org1"}
    assert await _dnd_scrubbed(attempt, _FakeCrm(dnd=True)) is True


async def test_dnd_scrubbed_false_for_clean_customer():
    attempt = {"id": "a1", "customer_id": "c1", "org_id": "org1"}
    assert await _dnd_scrubbed(attempt, _FakeCrm(dnd=False)) is False


async def test_dnd_scrubbed_false_when_no_customer_id():
    attempt = {"id": "a1", "customer_id": None, "org_id": "org1"}
    assert await _dnd_scrubbed(attempt, _FakeCrm(dnd=True)) is False


async def test_dnd_scrubbed_degrades_to_false_on_crm_failure():
    # A CRM outage must not starve the whole campaign — dial proceeds.
    attempt = {"id": "a1", "customer_id": "c1", "org_id": "org1"}
    assert await _dnd_scrubbed(attempt, _RaisingCrm()) is False
