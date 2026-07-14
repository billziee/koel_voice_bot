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
