"""Deepgram realtime STT provider (O-6 true-partials, nova-3).

All hermetic — NO live key. The WS transport is exercised by monkeypatching
``websockets.asyncio.client.connect`` to a fake socket that accepts the binary
PCM + ``CloseStream`` sends and replays scripted Deepgram JSON frames (2 interims
``is_final:false`` + 1 final ``is_final:true`` + a trailing ``Metadata``).

Four guarantees are pinned here:

1. ``stream_transcribe`` yields one non-final ``TranscriptResult`` per interim and
   a final one for the ``is_final`` result, over the verified protocol/URL/header.
2. Any WS error degrades to the fallback's batch result (never raises).
3. ``transcribe`` (batch) is a pure delegate to the fallback.
4. The WAV → linear16 PCM extraction returns the raw PCM + sample rate + channels,
   and rejects non-WAV audio (which makes the stream degrade to batch).

A fifth test proves the ``/v1/transcribe/stream`` endpoint wiring end-to-end with
a ``DeepgramSttProvider`` wrapping the stub.
"""

from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import pytest
import websockets.asyncio.client

from koel.common.auth import issue_token
from koel.common.schemas import Language
from koel.stt.providers import DeepgramSttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult

# --- Fixtures / helpers -------------------------------------------------------


def _wav(pcm: bytes = b"\x00\x01" * 8000, *, sample_rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # linear16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _req(audio: bytes | None = None, **kw) -> TranscribeRequest:  # noqa: ANN003
    audio = _wav() if audio is None else audio
    base = dict(session_id="s", audio_b64=base64.b64encode(audio).decode(),
                is_final=True, audio_format="wav")
    return TranscribeRequest(**{**base, **kw})


def _provider(**kw) -> DeepgramSttProvider:  # noqa: ANN003
    return DeepgramSttProvider(
        api_key="k", model="nova-3", fallback=StubWhisperProvider(), timeout_s=5.0, **kw
    )


class _FakeDeepgramWS:
    """Fake Deepgram realtime socket: records binary/text sends, replays frames."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.binary_sends: list[bytes] = []
        self.text_sends: list[str] = []

    async def __aenter__(self) -> _FakeDeepgramWS:
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    async def send(self, data) -> None:  # noqa: ANN001
        (self.text_sends if isinstance(data, str) else self.binary_sends).append(data)

    async def recv(self) -> str:
        if not self._frames:
            raise AssertionError("recv() past end of scripted frames")
        return self._frames.pop(0)


def _results(text: str, is_final: bool) -> str:
    return json.dumps(
        {
            "type": "Results",
            "channel": {"alternatives": [{"transcript": text, "confidence": 0.98}]},
            "is_final": is_final,
            "duration": 1.2,
        }
    )


def _interims_then_final_frames() -> list[str]:
    # Mirrors the live-verified shape: two interims, one final, then Metadata.
    return [
        _results("I want to know", False),
        _results("I want to know about the pre", False),
        _results("I want to know about the balance on my account.", True),
        json.dumps({"type": "Metadata", "request_id": "r"}),
    ]


# --- 1. Happy path: 2 interims + 1 final over the verified protocol -----------


async def test_stream_yields_two_interims_and_one_final(monkeypatch):
    fake = _FakeDeepgramWS(_interims_then_final_frames())
    captured: dict = {}

    def fake_connect(url, **kwargs):  # noqa: ANN001, ANN003
        captured["url"] = url
        captured["headers"] = kwargs.get("additional_headers")
        return fake

    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)

    results = [r async for r in _provider().stream_transcribe(_req())]

    non_final = [r for r in results if not r.is_final]
    final = [r for r in results if r.is_final]
    assert len(non_final) == 2
    assert len(final) == 1
    assert non_final[0].text == "I want to know"
    assert final[0].text == "I want to know about the balance on my account."
    assert final[0].word_confidence == 0.98
    assert final[0].speech_duration_ms == 1200  # from Deepgram's `duration`

    # Outbound: raw linear16 PCM streamed as binary frames, then a CloseStream flush.
    assert fake.binary_sends, "PCM must be streamed as binary WS frames"
    assert json.loads(fake.text_sends[-1]) == {"type": "CloseStream"}

    # URL + header follow the verified Deepgram realtime contract.
    assert captured["url"].startswith("wss://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in captured["url"]
    assert "encoding=linear16" in captured["url"]
    assert "sample_rate=16000" in captured["url"]
    assert "interim_results=true" in captured["url"]
    assert captured["headers"] == {"Authorization": "Token k"}


async def test_stream_carries_language_hint_through(monkeypatch):
    fake = _FakeDeepgramWS(_interims_then_final_frames())
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **k: fake)

    results = [r async for r in _provider().stream_transcribe(_req(language_hint=Language.hinglish))]

    # nova-3 returns no language id → the request hint is carried through.
    assert all(r.language_id == Language.hinglish for r in results)


async def test_stream_defaults_language_to_english_without_hint(monkeypatch):
    fake = _FakeDeepgramWS(_interims_then_final_frames())
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **k: fake)

    results = [r async for r in _provider().stream_transcribe(_req())]

    assert all(r.language_id == Language.en for r in results)  # English-centric model


async def test_stream_skips_empty_interim_frames(monkeypatch):
    frames = [
        _results("", False),  # empty interim (silence) → not emitted
        _results("hello there", True),
        json.dumps({"type": "Metadata"}),
    ]
    fake = _FakeDeepgramWS(frames)
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **k: fake)

    results = [r async for r in _provider().stream_transcribe(_req())]

    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text == "hello there"


# --- 2. WS error → degrade to the fallback batch, never raising ---------------


async def test_ws_error_degrades_to_fallback_never_raises(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("deepgram unreachable")

    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)

    req = _req()
    results = [r async for r in _provider().stream_transcribe(req)]

    # Degraded to the fallback stub's batch result: exactly one non-empty final.
    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text
    # And it equals the fallback's own batch transcribe (the delegation contract).
    batch = await StubWhisperProvider().transcribe(req)
    assert results[0].text == batch.text


async def test_ws_error_only_warns_once(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)
    p = _provider()

    _ = [r async for r in p.stream_transcribe(_req())]
    assert p._ws_warned is True  # noqa: SLF001
    # A second failing turn must not re-warn (log-once discipline like Sarvam).
    _ = [r async for r in p.stream_transcribe(_req())]
    assert p._ws_warned is True  # noqa: SLF001


# --- 3. Batch transcribe is a pure delegate + non-WAV degrades ----------------


async def test_batch_transcribe_delegates_to_fallback():
    p = _provider()
    req = _req()

    result = await p.transcribe(req)
    expected = await StubWhisperProvider().transcribe(req)

    assert result == expected  # deterministic stub → identical delegated result


async def test_type_to_talk_sentinel_skips_ws_and_uses_fallback(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("WS must not be opened for the TEXT: sentinel")

    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)
    audio = base64.b64encode(b"TEXT:haan mujhe interest hai").decode()
    req = TranscribeRequest(session_id="s", audio_b64=audio, is_final=True)

    results = [r async for r in _provider().stream_transcribe(req)]

    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text == "haan mujhe interest hai"


async def test_non_wav_audio_degrades_without_opening_ws(monkeypatch):
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("WS must not be opened for non-WAV audio")

    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)
    audio = base64.b64encode(b"\x11\x22\x33\x44" * 50).decode()  # no RIFF header
    req = TranscribeRequest(session_id="s", audio_b64=audio, is_final=True, audio_format="raw")

    results = [r async for r in _provider().stream_transcribe(req)]

    assert len(results) == 1 and results[0].is_final is True


# --- 4. WAV → linear16 PCM extraction -----------------------------------------


def test_extract_pcm_from_wav_returns_raw_pcm_rate_channels():
    pcm = b"\x01\x02\x03\x04" * 100
    wav = _wav(pcm, sample_rate=8000, channels=1)

    out_pcm, sample_rate, channels = DeepgramSttProvider._extract_pcm(wav)  # noqa: SLF001

    assert out_pcm == pcm  # RIFF header stripped, raw linear16 recovered
    assert sample_rate == 8000
    assert channels == 1


def test_extract_pcm_rejects_non_wav():
    with pytest.raises(ValueError, match="requires wav audio"):
        DeepgramSttProvider._extract_pcm(b"not a wav container at all")  # noqa: SLF001


def test_extract_pcm_rejects_non_16bit_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)  # 8-bit → unsupported
        wf.setframerate(16000)
        wf.writeframes(b"\x00" * 100)
    with pytest.raises(ValueError, match="16-bit"):
        DeepgramSttProvider._extract_pcm(buf.getvalue())  # noqa: SLF001


# --- 5. Endpoint wiring: /v1/transcribe/stream backed by Deepgram -------------


async def test_stream_endpoint_wires_deepgram_provider(monkeypatch):
    from koel.stt import main as stt_main

    fake = _FakeDeepgramWS(_interims_then_final_frames())
    monkeypatch.setattr(websockets.asyncio.client, "connect", lambda *a, **k: fake)
    monkeypatch.setattr(stt_main, "_provider", _provider())  # Deepgram wrapping the stub

    payload = {
        "session_id": "s",
        "audio_b64": base64.b64encode(_wav()).decode(),
        "is_final": True,
        "audio_format": "wav",
    }
    headers = {
        "Authorization": f"Bearer {issue_token('voicebot_svc', kind='access', role='service')}"
    }
    transport = httpx.ASGITransport(app=stt_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/transcribe/stream", json=payload, headers=headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    frames = [
        TranscriptResult.model_validate_json(line)
        for line in resp.text.splitlines()
        if line.strip()
    ]
    # Two interims then one final streamed straight through as NDJSON lines.
    assert [f.is_final for f in frames] == [False, False, True]
    assert frames[-1].text == "I want to know about the balance on my account."
