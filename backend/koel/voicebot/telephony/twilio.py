"""Twilio Media Streams protocol + webhook signature verification (1.1 / 1.3).

Media Streams is a bidirectional WebSocket of JSON frames: ``connected`` →
``start`` → many ``media`` (base64 8 kHz µ-law) + ``dtmf`` → ``stop``. Outbound,
the app sends ``media`` frames back and a ``clear`` frame to flush on barge-in.
This module parses/builds those frames and verifies Twilio's request signature;
the WS endpoint that wires them to the orchestrator is the remaining live step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass


@dataclass
class TwilioEvent:
    event: str  # connected | start | media | dtmf | mark | stop | (unknown)
    stream_sid: str | None = None
    payload_ulaw: bytes | None = None  # decoded inbound audio (media frames)
    digit: str | None = None  # pressed key (dtmf frames) — 1.3


def parse_twilio_message(raw: str) -> TwilioEvent:
    """Parse one inbound Media Streams frame into a typed event."""
    msg = json.loads(raw)
    event = str(msg.get("event", ""))
    stream_sid = msg.get("streamSid") or msg.get("start", {}).get("streamSid")
    if event == "media":
        payload = msg.get("media", {}).get("payload", "")
        # validate=True: a corrupt payload RAISES (→ the bridge's _on_frame skips the
        # frame) instead of silently dropping non-alphabet bytes and feeding mangled
        # audio into the decoder.
        return TwilioEvent(
            "media", stream_sid, payload_ulaw=base64.b64decode(payload, validate=True) if payload else b""
        )
    if event == "dtmf":
        return TwilioEvent("dtmf", stream_sid, digit=msg.get("dtmf", {}).get("digit"))
    return TwilioEvent(event, stream_sid)


def twilio_media_frame(stream_sid: str, ulaw: bytes) -> str:
    """Build an outbound media frame carrying 8 kHz µ-law audio."""
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": base64.b64encode(ulaw).decode()},
    })


def twilio_clear_frame(stream_sid: str) -> str:
    """Build a ``clear`` frame — flush buffered outbound audio on barge-in."""
    return json.dumps({"event": "clear", "streamSid": stream_sid})


def verify_twilio_signature(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    """Validate Twilio's ``X-Twilio-Signature`` (HMAC-SHA1 of the full URL plus
    the POST params concatenated in key-sorted order, base64). Fails CLOSED when
    no auth token is configured, so an unconfigured deployment rejects webhooks
    rather than trusting them."""
    if not auth_token:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")
