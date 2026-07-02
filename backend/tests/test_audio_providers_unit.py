"""TTS + STT provider units: stub synth/stream, prosody, degrade paths."""

from __future__ import annotations

import base64

import httpx

from koel.common.schemas import Language, Tone
from koel.stt.providers import GroqWhisperProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest
from koel.tts.mixer import wav_sample_rate
from koel.tts.providers import (
    GroqOrpheusProvider,
    StubOrpheusProvider,
    resolve_prosody,
)
from koel.tts.schemas import SynthesizeRequest


def _syn(**over) -> SynthesizeRequest:
    base = dict(session_id="s", text="Namaste, kaise hain aap?",
                language=Language.hinglish, tone=Tone.cheerful)
    base.update(over)
    return SynthesizeRequest(**base)


async def test_stub_synthesize_returns_wav_with_declared_rate():
    r = await StubOrpheusProvider().synthesize(_syn())
    wav = base64.b64decode(r.audio_b64)
    assert wav[:4] == b"RIFF" and wav_sample_rate(wav) == r.sample_rate
    assert r.duration_ms > 0


async def test_stub_synthesize_mixes_ambient_when_requested():
    plain = await StubOrpheusProvider().synthesize(_syn(ambient=False))
    mixed = await StubOrpheusProvider().synthesize(_syn(ambient=True, ambient_profile="callfloor"))
    # The ambient bed changes the bytes (a non-silent floor is mixed in).
    assert plain.audio_b64 != mixed.audio_b64


async def test_stub_stream_emits_multiple_chunks_ending_final():
    chunks = [c async for c in StubOrpheusProvider().stream(_syn())]
    assert len(chunks) >= 1
    assert chunks[-1].is_final is True
    assert all(not c.is_final for c in chunks[:-1])


def test_resolve_prosody_scales_with_speaking_rate():
    slow = resolve_prosody(_syn(speaking_rate=0.5))
    fast = resolve_prosody(_syn(speaking_rate=2.0))
    assert fast.rate > slow.rate


async def test_orpheus_degrades_to_stub_on_error(monkeypatch):
    p = GroqOrpheusProvider(api_key="k", model="m", voice="autumn")

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(p._client, "post", boom)
    r = await p.synthesize(_syn())
    assert r.audio_b64  # stub audio, never an exception
    await p.aclose()


async def test_stub_whisper_text_sentinel_passthrough():
    # "TEXT:..." keyboard path is decoded verbatim (demo type-to-talk).
    payload = base64.b64encode(b"TEXT:haan mujhe interest hai").decode()
    r = await StubWhisperProvider().transcribe(
        TranscribeRequest(session_id="s", audio_b64=payload, is_final=True)
    )
    assert "interest" in r.text and r.is_final is True


async def test_whisper_confidence_and_language_helpers():
    segs = [{"avg_logprob": -0.2, "no_speech_prob": 0.02}]
    assert 0.0 <= GroqWhisperProvider._confidence(segs) <= 1.0
    assert GroqWhisperProvider._confidence([]) == 0.9


async def test_whisper_degrades_to_stub_on_error(monkeypatch):
    p = GroqWhisperProvider(api_key="k", model="whisper-large-v3-turbo")

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(p._client, "post", boom)
    # Real (non-sentinel) audio bytes force the HTTP path, which fails → stub.
    r = await p.transcribe(
        TranscribeRequest(session_id="s", audio_b64=base64.b64encode(b"\x00\x01\x02" * 50).decode(),
                          is_final=True, audio_format="webm")
    )
    assert r.session_id == "s"  # degraded, not raised
    await p.aclose()
