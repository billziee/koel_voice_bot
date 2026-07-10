"""Streaming-STT seam (O-6): the batch-wrapping default and the Sarvam WS path.

Two guarantees are pinned here:

1. The ``SttProvider.stream_transcribe`` DEFAULT wraps batch ``transcribe`` and
   yields exactly one *final* result equal to it — so every provider satisfies
   the seam without a streaming implementation.
2. ``SarvamSttProvider.stream_transcribe`` streams partial+final transcripts over
   ``/speech-to-text/ws`` when enabled, and degrades to the batch path (never
   raising) when the WS connection fails — mirroring the TTS WS fallback.

The happy-path WS test runs a real local ``websockets`` server speaking the
protocol, matching ``test_sarvam_tts_ws`` rather than mocking the provider
against itself.
"""

from __future__ import annotations

import base64
import json

import httpx
import websockets.asyncio.client
from websockets.asyncio.server import serve

from koel.common.schemas import Language
from koel.stt.providers import SarvamSttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest


def _req(audio: bytes = b"\x00\x01" * 4000, **kw) -> TranscribeRequest:
    base = dict(session_id="s", audio_b64=base64.b64encode(audio).decode(),
                is_final=True, audio_format="wav")  # WS STT ingests WAV
    return TranscribeRequest(**{**base, **kw})


def _provider(*, stream_stt: bool = True, base_url: str = "https://api.sarvam.ai") -> SarvamSttProvider:
    return SarvamSttProvider(
        api_key="k",
        model="saaras:v3",
        base_url=base_url,
        fallback=StubWhisperProvider(),
        stream_stt=stream_stt,
        timeout_s=5.0,
    )


# --- 1. DEFAULT wrapper: one final result equal to batch transcribe() ---------


async def test_default_wrapper_yields_single_final_equal_to_batch():
    provider = StubWhisperProvider()
    request = _req()

    batch = await provider.transcribe(request)
    streamed = [r async for r in provider.stream_transcribe(request)]

    assert len(streamed) == 1
    assert streamed[0].is_final is True
    assert streamed[0] == batch  # deterministic stub → identical to the batch call


async def test_default_wrapper_forces_final_even_when_request_is_partial():
    # Seam contract: the single yielded result is always final, regardless of the
    # request's is_final (the consumer treats iterator termination as end-of-turn).
    provider = StubWhisperProvider()
    streamed = [r async for r in provider.stream_transcribe(_req(is_final=False))]

    assert len(streamed) == 1
    assert streamed[0].is_final is True


# --- 2. Sarvam WS happy path: partials then a final over a real local server --


async def test_sarvam_ws_transcribes_over_verified_protocol():
    # Protocol verified live against api.sarvam.ai (2026-07): a top-level `audio`
    # object (base64 WAV, WAV-only encoding) + `{"type":"flush"}` → a `data` frame.
    received: list[dict] = []

    async def handler(ws):
        received.append(json.loads(await ws.recv()))  # audio frame
        received.append(json.loads(await ws.recv()))  # flush
        await ws.send(
            json.dumps(
                {
                    "type": "data",
                    "data": {
                        "transcript": "namaste ji mujhe interest hai",
                        "language_code": "hi-IN",
                        "language_probability": 0.95,
                    },
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        p = _provider()
        p._ws_url = f"ws://127.0.0.1:{port}/speech-to-text/ws?model=saaras:v3&language_code=unknown"

        results = [r async for r in p.stream_transcribe(_req())]
        await p.aclose()

    # Outbound: a top-level `audio` object then a flush (no config frame).
    assert received[0]["audio"]["encoding"] == "audio/wav"
    assert received[0]["audio"]["data"]  # base64 audio payload streamed in
    assert received[0]["audio"]["sample_rate"] == 16000
    assert received[1] == {"type": "flush"}

    # Inbound: one authoritative final transcript.
    assert len(results) == 1 and results[0].is_final is True
    assert results[0].text == "namaste ji mujhe interest hai"
    assert results[0].language_id == Language.hi
    assert results[0].language_confidence == 0.95


# --- 3. Sarvam WS failure → degrade to the batch path, never raising ----------


async def test_sarvam_ws_failure_degrades_to_batch(monkeypatch):
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("ws unreachable")

    # _stream_ws lazily imports connect from this module; patch it to blow up.
    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)

    p = _provider()

    async def batch_ok(url, headers=None, files=None, data=None):  # noqa: ANN001
        payload = {"transcript": "batch transcript", "language_code": "hi-IN",
                   "language_probability": 0.88}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", batch_ok)

    results = [r async for r in p.stream_transcribe(_req())]
    await p.aclose()

    # WS blew up → the batch path still produced exactly one final transcript.
    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text == "batch transcript"


async def test_sarvam_ws_failure_cascades_to_stub_when_batch_also_fails(monkeypatch):
    # Belt-and-suspenders: WS refused AND the batch HTTP call unreachable →
    # Sarvam's own fallback (the deterministic stub) still yields a final.
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ConnectionRefusedError("ws unreachable")

    monkeypatch.setattr(websockets.asyncio.client, "connect", boom)

    p = _provider(base_url="http://127.0.0.1:1")  # batch POST is unreachable too
    results = [r async for r in p.stream_transcribe(_req())]
    await p.aclose()

    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text  # stub produced a deterministic transcript


# --- 4. Seam OFF: the WS path is never touched --------------------------------


async def test_sarvam_seam_off_skips_ws_and_uses_batch(monkeypatch):
    p = _provider(stream_stt=False)
    p._ws_url = "ws://127.0.0.1:1/would-explode-if-hit"

    async def batch_ok(url, headers=None, files=None, data=None):  # noqa: ANN001
        payload = {"transcript": "http path", "language_code": "en-IN",
                   "language_probability": 0.7}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", batch_ok)

    results = [r async for r in p.stream_transcribe(_req())]
    await p.aclose()

    assert len(results) == 1
    assert results[0].is_final is True
    assert results[0].text == "http path"
