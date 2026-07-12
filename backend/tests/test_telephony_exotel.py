"""Exotel Voice Streaming protocol + webhook signature tests (1.4).

Mirrors tests/test_telephony.py (the Twilio side). The live Exotel connection is
dormant (no account/keys), so these exercise the pure parse/build/verify
primitives: frame round-trips, event typing, defensive parsing, and the
fail-closed signature placeholder.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from koel.voicebot.telephony.exotel import (
    exotel_clear_frame,
    exotel_media_frame,
    parse_exotel_message,
    verify_exotel_signature,
)

# --- Exotel Voice Streaming protocol ----------------------------------------


def test_parse_media_frame_decodes_payload():
    audio = b"\x01\x02\x03"  # raw 8 kHz PCM16 bytes in the default media mode
    raw = json.dumps({
        "event": "media", "stream_sid": "SX1",
        "media": {"payload": base64.b64encode(audio).decode()},
    })
    ev = parse_exotel_message(raw)
    assert ev.event == "media" and ev.stream_sid == "SX1" and ev.payload_audio == audio


def test_media_frame_round_trips():
    audio = b"\x0a\x0b\x0c"
    frame = json.loads(exotel_media_frame("SX2", audio))
    assert frame["event"] == "media" and frame["stream_sid"] == "SX2"  # snake_case key
    assert base64.b64decode(frame["media"]["payload"]) == audio
    # build → parse round-trip recovers the stream sid and the decoded payload
    ev = parse_exotel_message(exotel_media_frame("SX2", audio))
    assert ev.event == "media" and ev.stream_sid == "SX2" and ev.payload_audio == audio
    assert json.loads(exotel_clear_frame("SX2")) == {"event": "clear", "stream_sid": "SX2"}


def test_parse_connected_start_dtmf_stop():
    connected = parse_exotel_message(json.dumps({"event": "connected"}))
    assert connected.event == "connected" and connected.stream_sid is None
    # start carries the stream sid NESTED under "start" (Exotel shape)
    start = parse_exotel_message(json.dumps({"event": "start", "start": {"stream_sid": "SX9"}}))
    assert start.event == "start" and start.stream_sid == "SX9"
    dtmf = parse_exotel_message(json.dumps({"event": "dtmf", "stream_sid": "SX9", "dtmf": {"digit": "5"}}))
    assert dtmf.event == "dtmf" and dtmf.digit == "5"
    stop = parse_exotel_message(json.dumps({"event": "stop", "stream_sid": "SX9"}))
    assert stop.event == "stop" and stop.stream_sid == "SX9"


def test_malformed_frames_never_raise():
    assert parse_exotel_message("not json at all").event == "unknown"
    assert parse_exotel_message("[1, 2, 3]").event == "unknown"  # valid JSON, but not an object
    assert parse_exotel_message("{}").event == "unknown"  # object with no event key
    # a media frame with a non-base64 payload degrades to empty bytes, not a crash
    bad = json.dumps({"event": "media", "stream_sid": "SX1", "media": {"payload": "!!!not-base64!!!"}})
    ev = parse_exotel_message(bad)
    assert ev.event == "media" and ev.stream_sid == "SX1" and ev.payload_audio == b""


# --- webhook signature (fail-closed HMAC placeholder) -----------------------


def test_signature_verifies_against_placeholder_algorithm():
    token = "test_auth_token"
    url = "https://example.com/telephony/exotel/voice"
    params = {"CallSid": "abc", "From": "+919000000000", "To": "+911111111111"}
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha256).digest()).decode()
    assert verify_exotel_signature(url, params, expected, token) is True
    assert verify_exotel_signature(url, params, "wrong", token) is False


def test_signature_fails_closed_without_a_token():
    assert verify_exotel_signature("https://x", {}, "anything", auth_token="") is False
