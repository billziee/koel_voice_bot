"""Tests for the Groq Whisper (STT) and Orpheus (TTS) providers — offline:
type-to-talk passthrough, WAV duration parsing, and graceful stub fallback."""

from __future__ import annotations

import base64
import struct

from koel.common.schemas import Language, Tone
from koel.stt.providers import GroqWhisperProvider
from koel.stt.schemas import TranscribeRequest
from koel.tts.providers import GroqOrpheusProvider, _wav_duration_ms
from koel.tts.schemas import SynthesizeRequest


def _wav(seconds: float, sr: int = 16000) -> bytes:
    n = int(sr * seconds)
    frames = b"\x00\x00" * n
    return (
        b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
        + b"data" + struct.pack("<I", len(frames)) + frames
    )


def test_wav_duration_parsed_from_header():
    assert abs(_wav_duration_ms(_wav(1.5)) - 1500) <= 2
    assert _wav_duration_ms(b"not a wav") == 0


def test_wav_duration_handles_orpheus_streaming_layout():
    # Orpheus: a LIST chunk before data + a 0xFFFFFFFF placeholder data size.
    sr = 24000
    frames = b"\x00\x00" * sr  # 1.0s @ 24kHz mono 16-bit
    wav = (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
        + b"LIST" + struct.pack("<I", 4) + b"INFO"
        + b"data" + struct.pack("<I", 0xFFFFFFFF) + frames
    )
    assert abs(_wav_duration_ms(wav) - 1000) <= 5  # not ~89,000,000


async def test_whisper_type_to_talk_passthrough_no_network():
    # A TEXT: sentinel must be served by the stub — no Groq call, deterministic.
    prov = GroqWhisperProvider(api_key="unused", model="whisper-large-v3-turbo")
    audio = base64.b64encode(b"TEXT:haan boliye sun raha hoon").decode()
    r = await prov.transcribe(
        TranscribeRequest(session_id="s", audio_b64=audio, is_final=True)
    )
    assert r.text == "haan boliye sun raha hoon"
    assert r.language_id == Language.hinglish
    await prov.aclose()


async def test_whisper_falls_back_to_stub_on_unreachable_host():
    # Real audio + dead endpoint → degrade to the stub, never raise.
    prov = GroqWhisperProvider(
        api_key="x", model="whisper-large-v3-turbo",
        base_url="http://127.0.0.1:1/v1", timeout_s=2,
    )
    audio = base64.b64encode(_wav(0.2)).decode()
    r = await prov.transcribe(TranscribeRequest(session_id="s", audio_b64=audio, is_final=True))
    assert r.is_final and r.text  # stub produced a deterministic transcript
    await prov.aclose()


async def test_orpheus_falls_back_to_stub_on_unreachable_host():
    prov = GroqOrpheusProvider(
        api_key="x", model="canopylabs/orpheus-v1-english", voice="tara",
        base_url="http://127.0.0.1:1/v1", timeout_s=2,
    )
    res = await prov.synthesize(
        SynthesizeRequest(session_id="s", text="Namaste ji", tone=Tone.cheerful)
    )
    # Stub fallback → sized silent PCM with a real duration.
    assert res.duration_ms > 0
    assert set(base64.b64decode(res.audio_b64)) == {0}
    await prov.aclose()
