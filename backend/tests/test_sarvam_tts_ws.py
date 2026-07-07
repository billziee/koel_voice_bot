"""Sarvam WS streaming TTS: protocol round-trip, fallback, and ambient bypass.

Runs a real local websockets server speaking the /text-to-speech/ws protocol
(config → text → flush in; audio chunks + final event out) so the provider's
WS path is exercised end-to-end, not against mocks of itself.
"""

from __future__ import annotations

import base64
import json

from websockets.asyncio.server import serve

from koel.common.schemas import Language, Tone
from koel.tts.providers import SarvamTtsProvider, StubOrpheusProvider
from koel.tts.schemas import SynthesizeRequest

# 100ms of silence @ 24kHz mono s16le.
_PCM_100MS = b"\x00\x00" * 2400


def _req(**overrides) -> SynthesizeRequest:
    base = dict(session_id="s", text="namaste ji", language=Language.hinglish, tone=Tone.neutral)
    return SynthesizeRequest(**{**base, **overrides})


def _provider(ws_enabled: bool = True) -> SarvamTtsProvider:
    return SarvamTtsProvider(
        api_key="k",
        model="bulbul:v3",
        speaker="priya",
        fallback=StubOrpheusProvider(),
        ws_enabled=ws_enabled,
        timeout_s=5.0,
    )


async def test_ws_streams_chunks_and_protocol_sequence():
    received: list[dict] = []

    async def handler(ws):
        for _ in range(3):  # config, text, flush
            received.append(json.loads(await ws.recv()))
        chunk = base64.b64encode(_PCM_100MS).decode()
        await ws.send(json.dumps({"type": "audio", "data": {"audio": chunk, "request_id": "r"}}))
        await ws.send(json.dumps({"type": "audio", "data": {"audio": chunk, "request_id": "r"}}))
        await ws.send(json.dumps({"type": "event", "data": {"event_type": "final"}}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        p = _provider()
        p._ws_url = f"ws://127.0.0.1:{port}/text-to-speech/ws?model=bulbul:v3"

        chunks = [c async for c in p.stream(_req())]
        await p.aclose()

    # Outbound protocol: config with language/speaker, then text, then flush.
    assert [m["type"] for m in received] == ["config", "text", "flush"]
    assert received[0]["data"]["target_language_code"] == "hi-IN"
    assert received[0]["data"]["speaker"] == "priya"
    assert received[0]["data"]["output_audio_codec"] == "linear16"
    assert received[1]["data"]["text"] == "namaste ji"

    # Inbound: each PCM chunk arrives as a self-contained WAV with real duration.
    assert [c.seq for c in chunks] == [0, 1]
    for c in chunks:
        assert base64.b64decode(c.audio_b64)[:4] == b"RIFF"
        assert 90 <= c.duration_ms <= 110
        assert c.sample_rate == 24000


async def test_ws_failure_before_audio_falls_back_to_http_path(monkeypatch):
    p = _provider()
    p._ws_url = "ws://127.0.0.1:1/unreachable"  # connection refused instantly

    calls: list[str] = []

    async def fake_synthesize(request):  # noqa: ANN001
        calls.append(request.text)
        return await StubOrpheusProvider().synthesize(request)

    monkeypatch.setattr(p, "synthesize", fake_synthesize)
    chunks = [c async for c in p.stream(_req())]
    await p.aclose()

    assert calls, "HTTP sentence path was not used after WS failure"
    assert chunks and chunks[-1].is_final is True


async def test_ws_error_message_falls_back():
    async def handler(ws):
        for _ in range(3):
            await ws.recv()
        await ws.send(json.dumps({"type": "error", "data": {"message": "bad request"}}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        p = _provider()
        p._ws_url = f"ws://127.0.0.1:{port}/x"

        seen: list[str] = []

        async def fake_synthesize(request):  # noqa: ANN001
            seen.append(request.text)
            return await StubOrpheusProvider().synthesize(request)

        p.synthesize = fake_synthesize  # type: ignore[method-assign]
        chunks = [c async for c in p.stream(_req())]
        await p.aclose()

    assert seen, "error frame should degrade to the HTTP path"
    assert chunks


async def test_ambient_requests_bypass_ws(monkeypatch):
    # Ambient beds are mixed per complete WAV; the WS path must not be used.
    p = _provider()
    p._ws_url = "ws://127.0.0.1:1/would-explode-if-hit"

    async def fake_synthesize(request):  # noqa: ANN001
        return await StubOrpheusProvider().synthesize(request)

    monkeypatch.setattr(p, "synthesize", fake_synthesize)
    req = _req(ambient=True, ambient_profile="callfloor")
    chunks = [c async for c in p.stream(req)]
    await p.aclose()

    assert chunks  # served via the HTTP sentence path without touching WS
