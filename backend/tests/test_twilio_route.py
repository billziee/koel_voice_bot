"""The /twilio/stream inbound bridge is DORMANT by default (roadmap 1.1).

Only the verifiable paths are covered here: with no auth token the endpoint must
fail closed, and a bad signature on a configured deployment must be rejected —
both BEFORE the socket is accepted, so neither touches the runtime/DB. A fully
connected happy path needs a valid Twilio signature + live provider stubs and a
running Media Streams peer, which isn't deterministically reproducible in-process.

These use Starlette's ``TestClient.websocket_connect`` (httpx's ASGITransport,
which backs the ``app_client`` fixture, does not speak the WebSocket protocol).
A pre-accept ``websocket.close(code=1008)`` surfaces client-side as a
``WebSocketDisconnect`` carrying that code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from koel.voicebot import main


def test_dormant_by_default_rejects_with_1008():
    # Default config: twilio_auth_token == "" ⇒ signature verification fails
    # closed ⇒ the upgrade is refused with policy-violation 1008.
    assert main.settings.twilio_auth_token == ""
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/twilio/stream"):
            pass
    assert exc.value.code == 1008


def test_bad_signature_is_rejected_when_token_configured(monkeypatch):
    # Token configured but the presented signature is wrong ⇒ still rejected 1008,
    # before accept — so a forged/replayed webhook never reaches the orchestrator.
    monkeypatch.setattr(main.settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(main.settings, "twilio_default_bot_id", "bot_test")
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/twilio/stream", headers={"X-Twilio-Signature": "not-a-valid-signature"}
        ):
            pass
    assert exc.value.code == 1008


def test_genuine_signature_with_query_params_clears_the_gate(monkeypatch):
    # Regression: a real Twilio Stream URL carries templated query params (e.g.
    # ``<Stream url=".../twilio/stream?From={{From}}">``). Twilio signs the FULL
    # url including that query string, appending NO extra params (there is no
    # POST body on this GET/WS-upgrade request). A correctly-computed signature
    # for such a URL must clear the signature gate — proving the endpoint no
    # longer re-sorts the query params and appends them as if they were POST
    # form data (which never matched Twilio's real digest). No bot is configured,
    # so the connection still closes 1008, but for the NEXT reason down the
    # chain ("inbound telephony disabled"), not "invalid twilio signature".
    monkeypatch.setattr(main.settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(main.settings, "public_base_url", "https://example.com")
    query = "From=%2B919000000000"
    signed_url = f"https://example.com/twilio/stream?{query}"
    digest = hmac.new(b"test-token", signed_url.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/twilio/stream?{query}", headers={"X-Twilio-Signature": signature}
        ):
            pass
    assert exc.value.code == 1008
    assert exc.value.reason == "inbound telephony disabled"


# --- /twilio/call-status webhook (1.2) --------------------------------------


def _twilio_signature(url: str, params: dict, token: str) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_call_status_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(main.settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(main.settings, "public_base_url", "https://example.com")
    client = TestClient(main.app)
    resp = client.post(
        "/twilio/call-status?attempt_id=a1",
        data={"CallStatus": "no-answer", "CallSid": "CA1"},
        headers={"X-Twilio-Signature": "not-a-valid-signature"},
    )
    assert resp.status_code == 403


async def test_call_status_webhook_records_no_answer_and_reschedules(monkeypatch, test_db):
    from koel.voicebot.campaigns import claim_due_attempts, enqueue_dial, get_attempt

    monkeypatch.setattr(main.settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(main.settings, "public_base_url", "https://example.com")

    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+911234567890"
    )
    await claim_due_attempts(limit=10)

    params = {"CallStatus": "no-answer", "CallSid": "CA1"}
    url = f"https://example.com/twilio/call-status?attempt_id={attempt_id}"
    signature = _twilio_signature(url, params, "test-token")

    client = TestClient(main.app)
    resp = client.post(
        f"/twilio/call-status?attempt_id={attempt_id}",
        data=params,
        headers={"X-Twilio-Signature": signature},
    )
    assert resp.status_code == 200, resp.text

    updated = await get_attempt(attempt_id)
    assert updated["status"] == "pending"  # no-answer -> retryable, rescheduled
    assert updated["last_outcome"] == "no_answer"


async def test_call_status_webhook_ignores_non_final_status(monkeypatch, test_db):
    from koel.voicebot.campaigns import claim_due_attempts, enqueue_dial, get_attempt

    monkeypatch.setattr(main.settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(main.settings, "public_base_url", "https://example.com")

    attempt_id = await enqueue_dial(
        campaign_id=None, org_id="org1", customer_id="c1", phone="+911234567890"
    )
    await claim_due_attempts(limit=10)

    params = {"CallStatus": "in-progress", "CallSid": "CA1"}
    url = f"https://example.com/twilio/call-status?attempt_id={attempt_id}"
    signature = _twilio_signature(url, params, "test-token")

    client = TestClient(main.app)
    resp = client.post(
        f"/twilio/call-status?attempt_id={attempt_id}",
        data=params,
        headers={"X-Twilio-Signature": signature},
    )
    assert resp.status_code == 200, resp.text

    # "in-progress" isn't a final outcome — the attempt stays untouched
    # (still in_progress, no last_outcome), left for twilio_stream's own
    # teardown to report the real outcome once the call actually ends.
    updated = await get_attempt(attempt_id)
    assert updated["status"] == "in_progress"
    assert updated["last_outcome"] is None
