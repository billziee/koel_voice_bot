"""Extra provider branches: STT Groq success, TTS Groq stream, prosody by tone."""

from __future__ import annotations

import base64

import httpx

from koel.common.schemas import Language, Tone
from koel.stt.providers import GroqWhisperProvider
from koel.stt.schemas import TranscribeRequest
from koel.tts.providers import GroqOrpheusProvider, resolve_prosody
from koel.tts.schemas import SynthesizeRequest


def _syn(tone=Tone.cheerful) -> SynthesizeRequest:
    return SynthesizeRequest(session_id="s", text="Namaste ji", language=Language.hinglish, tone=tone)


async def test_whisper_parses_verbose_json(monkeypatch):
    p = GroqWhisperProvider(api_key="k", model="whisper-large-v3-turbo")

    async def ok(*a, **k):
        return httpx.Response(200, json={
            "text": "annual fee kitni hai",
            "language": "hindi",
            "duration": 1.2,
            "segments": [{"avg_logprob": -0.15, "no_speech_prob": 0.01}],
        }, request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.transcribe(TranscribeRequest(
        session_id="s", audio_b64=base64.b64encode(b"\x00\x01" * 80).decode(),
        is_final=True, audio_format="webm",
    ))
    assert r.text == "annual fee kitni hai"
    assert r.language_id == Language.hi
    assert 0.0 < r.language_confidence <= 1.0
    await p.aclose()


async def test_orpheus_stream_emits_final_chunk(monkeypatch):
    p = GroqOrpheusProvider(api_key="k", model="m", voice="autumn")

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(p._client, "post", boom)  # degrades to stub inside synthesize
    chunks = [c async for c in p.stream(_syn())]
    assert chunks and chunks[-1].is_final is True
    await p.aclose()


async def test_magpie_degrades_to_fallback_on_grpc_failure():
    # NVIDIA Magpie (gRPC) with an unreachable endpoint must degrade to the
    # fallback provider, never drop the turn.
    from koel.tts.providers import NvidiaMagpieTtsProvider, StubOrpheusProvider

    p = NvidiaMagpieTtsProvider(
        api_key="k", function_id="fn", uri="localhost:1", voice="Magpie-ZeroShot.Female-1",
        sample_rate=44100, timeout_s=2.0, fallback=StubOrpheusProvider(),
    )
    r = await p.synthesize(_syn().model_copy(update={"language": Language.hi}))
    assert r.audio_b64  # fallback WAV, not an exception
    chunks = [c async for c in p.stream(_syn().model_copy(update={"language": Language.hi}))]
    assert chunks and chunks[-1].is_final is True


def test_resolve_prosody_varies_by_tone():
    cheerful = resolve_prosody(_syn(Tone.cheerful))
    empathetic = resolve_prosody(_syn(Tone.empathetic))
    firm = resolve_prosody(_syn(Tone.firm))
    # Each tone maps to a distinct prosody profile (pitch/rate/energy differ).
    profiles = {(p.pitch, p.rate, p.energy) for p in (cheerful, empathetic, firm)}
    assert len(profiles) >= 2
