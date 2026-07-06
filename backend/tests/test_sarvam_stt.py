"""Sarvam Saaras STT provider: happy path, auto-detect confidence, and fallback cascade."""

from __future__ import annotations

import base64

import httpx

from koel.common.schemas import Language
from koel.stt.providers import GroqWhisperProvider, SarvamSttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest


def _req(audio: bytes, **kw) -> TranscribeRequest:
    return TranscribeRequest(
        session_id="s", audio_b64=base64.b64encode(audio).decode(), is_final=True, **kw
    )


async def test_sarvam_transcribes_and_maps_detected_language(monkeypatch):
    p = SarvamSttProvider(api_key="k", model="saaras:v3", fallback=StubWhisperProvider())
    captured: dict = {}

    async def ok(url, headers=None, files=None, data=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        payload = {
            "request_id": "r1",
            "transcript": "namaste, mujhe interest hai",
            "language_code": "hi-IN",
            "language_probability": 0.92,
        }
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.transcribe(_req(b"\x00\x01" * 4000))

    assert captured["url"].endswith("/speech-to-text")
    assert captured["headers"]["api-subscription-key"] == "k"
    assert captured["data"]["language_code"] == "unknown"  # auto-detect, not forced
    assert r.text == "namaste, mujhe interest hai"
    assert r.language_id == Language.hi
    assert r.language_confidence == 0.92
    await p.aclose()


async def test_sarvam_null_probability_defaults_to_midpoint_confidence(monkeypatch):
    p = SarvamSttProvider(api_key="k", model="saaras:v3", fallback=StubWhisperProvider())

    async def ok(url, headers=None, files=None, data=None):  # noqa: ANN001
        payload = {"transcript": "hello", "language_code": "en-IN", "language_probability": None}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.transcribe(_req(b"\x00\x01" * 4000))
    assert r.language_confidence == 0.5
    await p.aclose()


async def test_sarvam_unrecognized_language_falls_back_to_hint(monkeypatch):
    p = SarvamSttProvider(api_key="k", model="saaras:v3", fallback=StubWhisperProvider())

    async def ok(url, headers=None, files=None, data=None):  # noqa: ANN001
        payload = {"transcript": "...", "language_code": "mr-IN", "language_probability": 0.4}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.transcribe(_req(b"\x00\x01" * 4000, language_hint=Language.hinglish))
    assert r.language_id == Language.hinglish
    await p.aclose()


async def test_sarvam_type_to_talk_skips_network_and_uses_fallback():
    p = SarvamSttProvider(api_key="k", model="saaras:v3", fallback=StubWhisperProvider())
    audio = base64.b64encode(b"TEXT:haan boliye").decode()
    r = await p.transcribe(
        TranscribeRequest(session_id="s", audio_b64=audio, is_final=True)
    )
    assert r.text == "haan boliye"
    await p.aclose()


async def test_sarvam_cascades_through_whisper_to_stub_on_failure():
    # Sarvam unreachable → falls to Whisper (also given an unreachable host) →
    # falls to the deterministic stub. A live call never drops.
    whisper = GroqWhisperProvider(
        api_key="x", model="whisper-large-v3-turbo", base_url="http://127.0.0.1:1/v1", timeout_s=2,
    )
    p = SarvamSttProvider(
        api_key="k", model="saaras:v3", base_url="http://127.0.0.1:1", timeout_s=2, fallback=whisper,
    )
    r = await p.transcribe(_req(b"\x00\x01" * 4000))
    assert r.is_final and r.text  # stub produced a deterministic transcript
    await p.aclose()
    await whisper.aclose()
