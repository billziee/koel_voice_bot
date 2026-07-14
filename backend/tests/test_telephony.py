"""Telephony foundation — codec, Twilio protocol, signature (1.1 / 1.3)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from koel.voicebot.telephony.codec import pcm16_to_ulaw8k, ulaw8k_to_pcm16
from koel.voicebot.telephony.twilio import (
    parse_twilio_message,
    twilio_clear_frame,
    twilio_media_frame,
    verify_twilio_signature,
)

# --- codec ------------------------------------------------------------------

def test_pcm_to_ulaw_halves_bytes_and_survives_round_trip():
    pcm = b"\x00\x01" * 800  # 1600 bytes = 800 samples @ 16-bit
    ulaw = pcm16_to_ulaw8k(pcm, sample_rate=8000)
    assert len(ulaw) == 800  # µ-law is 1 byte/sample
    back = ulaw8k_to_pcm16(ulaw, target_rate=8000)
    assert len(back) == 1600  # decoded back to 16-bit PCM, same sample count


def test_resample_16k_pcm_to_8k_ulaw():
    pcm16k = b"\x10\x00" * 1600  # 1600 samples @ 16 kHz = 100ms
    ulaw = pcm16_to_ulaw8k(pcm16k, sample_rate=16000)
    assert len(ulaw) == 800  # downsampled to 8 kHz → 800 samples in 100ms


def test_inbound_ulaw_decodes_to_16k_for_stt():
    ulaw = b"\xff" * 800  # 100ms of 8 kHz µ-law
    pcm = ulaw8k_to_pcm16(ulaw, target_rate=16000)
    # ~1600 samples @ 16-bit @ 16 kHz (≈3200 bytes; ratecv has a 1-sample edge).
    assert abs(len(pcm) - 3200) <= 4 and len(pcm) % 2 == 0


# --- Twilio Media Streams protocol -----------------------------------------

def test_parse_media_frame_decodes_payload():
    ulaw = b"\x01\x02\x03"
    raw = json.dumps({
        "event": "media", "streamSid": "MZ1",
        "media": {"payload": base64.b64encode(ulaw).decode()},
    })
    ev = parse_twilio_message(raw)
    assert ev.event == "media" and ev.stream_sid == "MZ1" and ev.payload_ulaw == ulaw


def test_parse_start_and_dtmf_and_stop():
    start = parse_twilio_message(json.dumps({"event": "start", "start": {"streamSid": "MZ9"}}))
    assert start.event == "start" and start.stream_sid == "MZ9"
    dtmf = parse_twilio_message(json.dumps({"event": "dtmf", "streamSid": "MZ9", "dtmf": {"digit": "1"}}))
    assert dtmf.event == "dtmf" and dtmf.digit == "1"  # 1.3 DTMF capture
    stop = parse_twilio_message(json.dumps({"event": "stop", "streamSid": "MZ9"}))
    assert stop.event == "stop"


def test_media_frame_round_trips():
    ulaw = b"\x0a\x0b\x0c"
    frame = json.loads(twilio_media_frame("MZ2", ulaw))
    assert frame["event"] == "media" and frame["streamSid"] == "MZ2"
    assert base64.b64decode(frame["media"]["payload"]) == ulaw
    assert json.loads(twilio_clear_frame("MZ2")) == {"event": "clear", "streamSid": "MZ2"}


# --- webhook signature ------------------------------------------------------

def test_signature_verifies_against_twilios_algorithm():
    token = "test_auth_token"
    url = "https://example.com/telephony/twilio/voice"
    params = {"CallSid": "CA123", "From": "+919000000000", "To": "+911111111111"}
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    assert verify_twilio_signature(url, params, expected, token) is True
    assert verify_twilio_signature(url, params, "wrong", token) is False


def test_signature_fails_closed_without_a_token():
    assert verify_twilio_signature("https://x", {}, "anything", auth_token="") is False
