"""Exotel Voice Streaming (Voicebot Applet) protocol + webhook signature (1.4).

KOEL already bridges Twilio Media Streams (1.1); this is the Exotel equivalent so
Indian-PSTN calls terminate on India-hosted infrastructure (data-residency + cost
win) through the SAME ``CarrierBridge`` contract — everything above the bridge
(orchestrator, STT/NLP/TTS, barge-in) is shared and only the carrier-specific WS
framing differs. The live Exotel connection is DORMANT here (no account/keys):
this module provides the frame parse/build + signature primitives only, mirroring
``twilio.py`` in shape, so wiring the WS endpoint is the remaining live step.

Wire protocol (Exotel Voice Streaming — a bidirectional WebSocket of JSON frames):
``connected`` → ``start`` → many ``media`` (base64 8 kHz audio) + ``dtmf`` →
``stop``; outbound the app sends ``media`` frames back and a ``clear`` frame to
flush on barge-in. Exotel mirrors Twilio Media Streams closely, with two
documented differences that shape this adapter:

  * frame keys are snake_case (``stream_sid``) not camelCase (``streamSid``), and
    the start frame carries the id nested under ``start.stream_sid``;
  * default audio is raw 8 kHz 16-bit mono PCM (``slin`` / ``raw``), base64
    encoded — NOT µ-law. So NO µ-law transcode is needed in the default mode:
    inbound ``payload_audio`` bytes are already PCM16 ready for STT, and
    synthesized PCM (resampled to 8 kHz) can be framed straight back. If an
    account is instead provisioned for µ-law, transcode via ``codec.py``
    (``pcm16_to_ulaw8k`` / ``ulaw8k_to_pcm16``) — the framing functions below are
    codec-blind (base64 only), so the mode lives at the bridge boundary and is
    documented by ``DEFAULT_MEDIA_MODE``.

ASSUMPTIONS TO VERIFY against live Exotel docs (account-gated, unverifiable here):

  1. snake_case ``stream_sid`` at top level + nested ``start.stream_sid`` on the
     start frame; ``media.payload`` base64; ``dtmf.digit`` for keypresses.
  2. media payload is base64 raw PCM16 @ 8 kHz mono in the default mode.
  3. Exotel's webhook auth scheme: Exotel commonly secures callbacks via HTTP
     Basic Auth embedded in the callback URL or via IP allowlisting rather than a
     Twilio-style HMAC signature header. ``verify_exotel_signature`` below is a
     FAIL-CLOSED HMAC PLACEHOLDER shaped like ``verify_twilio_signature``; the
     exact algorithm MUST be confirmed + replaced once Exotel's scheme is known.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal

from koel.common.logging import get_logger

logger = get_logger("koel.voicebot.telephony.exotel")

# Exotel's default media codec is raw 8 kHz 16-bit mono PCM ("slin"/"raw"); some
# accounts are provisioned for µ-law instead. The framing functions here are
# codec-blind, so this only tells the bridge layer whether to transcode via
# codec.py. Overridable via config (``exotel_media_mode``, default "pcm").
MediaMode = Literal["pcm", "ulaw"]
DEFAULT_MEDIA_MODE: MediaMode = "pcm"


@dataclass
class ExotelEvent:
    event: str  # connected | start | media | dtmf | mark | stop | (unknown)
    stream_sid: str | None = None
    # Decoded inbound audio (media frames): raw 8 kHz PCM16 in the default mode,
    # µ-law if the account is provisioned for it — see DEFAULT_MEDIA_MODE.
    payload_audio: bytes | None = None
    digit: str | None = None  # pressed key (dtmf frames)


def parse_exotel_message(raw: str) -> ExotelEvent:
    """Parse one inbound Voice Streaming frame into a typed event.

    Defensive by contract (1.4): malformed JSON, a non-object body, or an
    unexpected shape yields an ``"unknown"`` event and is logged — never raised.
    A carrier must not be able to crash the WS handler with a bad frame.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("exotel: dropping unparseable frame: %s", exc)
        return ExotelEvent("unknown")
    if not isinstance(msg, dict):
        logger.warning("exotel: dropping non-object frame (%s)", type(msg).__name__)
        return ExotelEvent("unknown")

    event = str(msg.get("event", ""))
    stream_sid = _extract_stream_sid(msg)
    if event == "media":
        return ExotelEvent("media", stream_sid, payload_audio=_decode_payload(msg.get("media")))
    if event == "dtmf":
        return ExotelEvent("dtmf", stream_sid, digit=_extract_digit(msg.get("dtmf")))
    return ExotelEvent(event or "unknown", stream_sid)


def exotel_media_frame(stream_sid: str, pcm_or_ulaw: bytes) -> str:
    """Build an outbound media frame carrying 8 kHz audio (base64).

    ``pcm_or_ulaw`` is already in the account's negotiated codec — raw 16-bit PCM
    by default (see module docstring) or µ-law if provisioned. Codec-blind: this
    only base64-encodes, exactly like ``twilio_media_frame``.
    """
    return json.dumps({
        "event": "media",
        "stream_sid": stream_sid,
        "media": {"payload": base64.b64encode(pcm_or_ulaw).decode()},
    })


def exotel_clear_frame(stream_sid: str) -> str:
    """Build a ``clear`` frame — flush buffered outbound audio on barge-in (PRD §05)."""
    return json.dumps({"event": "clear", "stream_sid": stream_sid})


def verify_exotel_signature(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    """Validate an Exotel webhook signature — FAIL-CLOSED PLACEHOLDER (1.4).

    Exotel's production callbacks are commonly secured with HTTP Basic Auth in the
    callback URL or with IP allowlisting rather than a Twilio-style HMAC header, so
    the exact signing scheme MUST be confirmed against Exotel's docs before this is
    trusted live. Until then it mirrors ``verify_twilio_signature``: HMAC (SHA-256)
    of the URL plus the params concatenated in key-sorted order, base64, compared
    in constant time. Critically it returns ``False`` whenever no auth token is
    configured, so an unconfigured (dormant) deployment REJECTS webhooks rather
    than trusting them.
    """
    if not auth_token:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


# --- internal helpers -------------------------------------------------------


def _extract_stream_sid(msg: dict[str, object]) -> str | None:
    """Exotel carries ``stream_sid`` at the top level and (on the start frame)
    nested under ``start``. camelCase is accepted defensively too."""
    direct = msg.get("stream_sid") or msg.get("streamSid")
    if isinstance(direct, str):
        return direct
    start = msg.get("start")
    if isinstance(start, dict):
        nested = start.get("stream_sid") or start.get("streamSid")
        if isinstance(nested, str):
            return nested
    return None


def _extract_digit(dtmf: object) -> str | None:
    """Pull the pressed key from a dtmf frame; tolerate a missing/odd shape."""
    if isinstance(dtmf, dict):
        digit = dtmf.get("digit")
        if isinstance(digit, str):
            return digit
    return None


def _decode_payload(media: object) -> bytes:
    """Base64-decode a media frame's payload; absent/empty/invalid → ``b""``.
    Never raises — a corrupt payload degrades to silence, not a crash."""
    if not isinstance(media, dict):
        return b""
    payload = media.get("payload")
    if not isinstance(payload, str) or not payload:
        return b""
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        logger.warning("exotel: media payload is not valid base64: %s", exc)
        return b""
