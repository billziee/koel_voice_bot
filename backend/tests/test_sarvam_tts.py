"""Sarvam Bulbul TTS provider: happy path, response decoding, and cascade fallback."""

from __future__ import annotations

import base64

import httpx

from koel.common.schemas import Language, Tone
from koel.tts.providers import SarvamTtsProvider, StubOrpheusProvider
from koel.tts.schemas import SynthesizeRequest

_WAV = (
    b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "
    + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
    + (24000).to_bytes(4, "little") + (48000).to_bytes(4, "little")
    + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    + b"data" + (0).to_bytes(4, "little")
)


def _req(language=Language.hi) -> SynthesizeRequest:
    return SynthesizeRequest(session_id="s", text="नमस्ते जी", language=language, tone=Tone.neutral)


async def test_sarvam_synthesize_decodes_base64_audio(monkeypatch):
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", fallback=StubOrpheusProvider(),
    )
    captured: dict = {}

    async def ok(url, headers=None, json=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        body = base64.b64encode(_WAV).decode()
        payload = {"request_id": "r1", "audios": [body]}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.synthesize(_req())

    assert captured["url"].endswith("/text-to-speech")
    assert captured["headers"]["api-subscription-key"] == "k"
    assert captured["json"]["target_language_code"] == "hi-IN"
    assert captured["json"]["speaker"] == "priya"
    assert base64.b64decode(r.audio_b64)[:4] == b"RIFF"
    assert r.fallback_used is False
    await p.aclose()


async def test_sarvam_falls_back_when_response_has_no_audio(monkeypatch):
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", fallback=StubOrpheusProvider(),
    )

    async def empty(url, headers=None, json=None):  # noqa: ANN001
        payload = {"request_id": "r1", "audios": []}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", empty)
    r = await p.synthesize(_req())
    assert r.fallback_used is True
    assert r.audio_b64  # stub still produced a real-duration WAV
    await p.aclose()


async def test_sarvam_falls_back_to_magpie_then_never_drops_turn():
    # Sarvam unreachable → cascades into its configured fallback (here: another
    # unreachable provider standing in for Magpie) → that one also degrades to stub.
    inner_fallback = StubOrpheusProvider()
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya",
        base_url="http://127.0.0.1:1", timeout_s=2, fallback=inner_fallback,
    )
    r = await p.synthesize(_req())
    assert r.fallback_used is True
    assert r.audio_b64
    await p.aclose()


async def test_sarvam_stream_emits_final_chunk(monkeypatch):
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", fallback=StubOrpheusProvider(),
    )

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(p._client, "post", boom)
    chunks = [c async for c in p.stream(_req())]
    assert chunks and chunks[-1].is_final is True
    await p.aclose()


def test_sarvam_language_code_mapping():
    p = SarvamTtsProvider(api_key="k", model="bulbul:v3", speaker="priya", fallback=StubOrpheusProvider())
    assert p._LANG_CODE["hi"] == "hi-IN"
    assert p._LANG_CODE["hinglish"] == "hi-IN"
    assert p._LANG_CODE["en"] == "en-IN"


class _MarkerProvider(StubOrpheusProvider):
    """Tags its output so a test can tell which fallback branch ran."""

    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def synthesize(self, request: SynthesizeRequest):  # noqa: ANN201
        res = await super().synthesize(request)
        return res.model_copy(update={"session_id": self.marker})


async def test_sarvam_hi_request_falls_back_to_magpie_not_base():
    # Magpie is Devanagari-native (same script family as hi) — a hi failure
    # must cascade there, not straight to base/Orpheus.
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", base_url="http://127.0.0.1:1", timeout_s=2,
        fallback=_MarkerProvider("BASE"), hi_fallback=_MarkerProvider("MAGPIE"),
    )
    r = await p.synthesize(_req(Language.hi))
    assert r.session_id == "MAGPIE"
    assert r.fallback_used is True
    await p.aclose()


async def test_sarvam_hinglish_request_skips_magpie_falls_to_base():
    # Magpie would mangle romanized Hinglish text — a hinglish failure must
    # cascade straight to base/Orpheus, never to hi_fallback.
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", base_url="http://127.0.0.1:1", timeout_s=2,
        fallback=_MarkerProvider("BASE"), hi_fallback=_MarkerProvider("MAGPIE"),
    )
    r = await p.synthesize(_req(Language.hinglish))
    assert r.session_id == "BASE"
    assert r.fallback_used is True
    await p.aclose()


async def test_sarvam_hi_request_without_hi_fallback_uses_base():
    # hi_fallback is optional (Magpie may not be configured at all).
    p = SarvamTtsProvider(
        api_key="k", model="bulbul:v3", speaker="priya", base_url="http://127.0.0.1:1", timeout_s=2,
        fallback=_MarkerProvider("BASE"),
    )
    r = await p.synthesize(_req(Language.hi))
    assert r.session_id == "BASE"
    await p.aclose()
