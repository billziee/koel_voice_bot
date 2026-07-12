"""STT_svc ``POST /v1/transcribe/stream`` (O-6): the NDJSON streaming endpoint
that backs ws-mode bots.

Two guarantees are pinned here, both against the real STT app stood up over
ASGITransport with the deterministic stub provider (no live key):

1. The endpoint yields NDJSON ``TranscriptResult`` frame(s) ending in a final —
   for the stub, exactly one final equal to the batch result.
2. If the provider's ``stream_transcribe`` raises before producing a final, the
   endpoint degrades to a single batch ``transcribe()`` so it ALWAYS yields a
   final (a ws-mode turn is never dropped).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import httpx

from koel.common.auth import issue_token
from koel.stt.providers import StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult


def _svc_headers() -> dict[str, str]:
    # ServicePrincipal auth: a role=service access token (same default jwt secret
    # the app decodes with).
    return {"Authorization": f"Bearer {issue_token('voicebot_svc', kind='access', role='service')}"}


def _payload(text: str = "haan mujhe interest hai") -> dict:
    # TEXT: sentinel drives a deterministic stub transcript without a mic/model.
    audio = base64.b64encode(f"TEXT:{text}".encode()).decode()
    return {"session_id": "s", "audio_b64": audio, "is_final": True, "audio_format": "wav"}


async def _post_stream(monkeypatch, provider) -> httpx.Response:  # noqa: ANN001
    from koel.stt import main as stt_main

    monkeypatch.setattr(stt_main, "_provider", provider)
    transport = httpx.ASGITransport(app=stt_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/transcribe/stream", json=_payload(), headers=_svc_headers())


def _frames(resp: httpx.Response) -> list[TranscriptResult]:
    return [TranscriptResult.model_validate_json(line) for line in resp.text.splitlines() if line.strip()]


async def test_stream_endpoint_yields_ndjson_ending_in_final(monkeypatch):
    resp = await _post_stream(monkeypatch, StubWhisperProvider())

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    frames = _frames(resp)
    assert frames, "endpoint must yield at least one frame"
    assert frames[-1].is_final is True
    assert frames[-1].text == "haan mujhe interest hai"

    # The stub's streaming seam wraps batch: exactly one final equal to transcribe().
    batch = await StubWhisperProvider().transcribe(TranscribeRequest(**_payload()))
    assert len(frames) == 1 and frames[0] == batch


async def test_stream_endpoint_degrades_to_batch_when_stream_raises(monkeypatch):
    class _BrokenStream(StubWhisperProvider):
        async def stream_transcribe(self, request) -> AsyncIterator[TranscriptResult]:  # noqa: ANN001
            raise RuntimeError("ws exploded")
            yield  # pragma: no cover — makes this an async generator

    resp = await _post_stream(monkeypatch, _BrokenStream())

    assert resp.status_code == 200
    frames = _frames(resp)
    # The stream raised before any final → the endpoint fell back to batch and
    # still produced exactly one final so the turn is never dropped.
    assert len(frames) == 1
    assert frames[0].is_final is True
    assert frames[0].text == "haan mujhe interest hai"
