"""OrpheusSageMakerProvider: boto3 SageMaker-runtime invoke_endpoint mechanics.

Hermetic — boto3.client is monkeypatched so no AWS credentials/network are
touched. No SageMaker endpoint is live yet, so the request/response JSON+audio
shape asserted here is this file's ASSUMED convention (see
``OrpheusSageMakerProvider._invoke_endpoint`` docstring), not a fixed spec —
these tests pin the assumed contract, not a verified one.
"""

from __future__ import annotations

import base64
import json

import boto3

from koel.common.schemas import Language, Tone
from koel.tts.providers import OrpheusSageMakerProvider, _pcm_to_wav
from koel.tts.schemas import CANONICAL_SAMPLE_RATE, SynthesizeRequest


def _req(text: str = "hello there", **kw) -> SynthesizeRequest:
    return SynthesizeRequest(session_id="s", text=text, language=Language.en, tone=Tone.neutral, **kw)


class _FakeBody:
    """Stands in for the streaming-body object botocore returns in ``Body``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeSageMakerClient:
    def __init__(self, response: dict | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def invoke_endpoint(self, **kwargs):  # noqa: ANN201 - mirrors botocore's untyped return
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


def _make_provider(monkeypatch, fake_client: _FakeSageMakerClient) -> OrpheusSageMakerProvider:
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake_client)
    return OrpheusSageMakerProvider(endpoint_name="orpheus-ep", region="ap-south-1")


def _wav(seconds: float = 0.1) -> bytes:
    pcm = b"\x00\x01" * int(CANONICAL_SAMPLE_RATE * seconds)
    return _pcm_to_wav(pcm, CANONICAL_SAMPLE_RATE)


async def test_sagemaker_synthesize_maps_response_to_synthesis_result(monkeypatch):
    wav = _wav()
    fake_client = _FakeSageMakerClient(response={"Body": _FakeBody(wav), "ContentType": "audio/wav"})
    p = _make_provider(monkeypatch, fake_client)

    result = await p.synthesize(_req("namaste"))

    assert result.audio_b64 == base64.b64encode(wav).decode()
    assert result.duration_ms > 0
    assert result.sample_rate == CANONICAL_SAMPLE_RATE
    assert result.audio_format == "wav"
    assert result.fallback_used is False

    assert fake_client.calls[0]["EndpointName"] == "orpheus-ep"
    assert fake_client.calls[0]["ContentType"] == "application/json"
    body = json.loads(fake_client.calls[0]["Body"])
    assert body["text"] == "namaste"
    assert body["language"] == "en"
    assert "speed" in body


async def test_sagemaker_synthesize_parses_content_type_subtype(monkeypatch):
    wav = _wav()
    fake_client = _FakeSageMakerClient(
        response={"Body": _FakeBody(wav), "ContentType": "audio/x-custom; charset=binary"}
    )
    p = _make_provider(monkeypatch, fake_client)

    result = await p.synthesize(_req())

    assert result.audio_format == "x-custom"


async def test_sagemaker_client_built_with_correct_service_and_region(monkeypatch):
    calls: list[tuple] = []

    def fake_client(service_name, **kwargs):  # noqa: ANN001
        calls.append((service_name, kwargs))
        return _FakeSageMakerClient(response={"Body": _FakeBody(_wav()), "ContentType": "audio/wav"})

    monkeypatch.setattr(boto3, "client", fake_client)
    OrpheusSageMakerProvider(endpoint_name="orpheus-ep", region="ap-south-1")

    assert calls[0][0] == "sagemaker-runtime"
    assert calls[0][1]["region_name"] == "ap-south-1"


async def test_sagemaker_synthesize_falls_back_on_invoke_error(monkeypatch):
    fake_client = _FakeSageMakerClient(exc=RuntimeError("endpoint not found"))
    p = _make_provider(monkeypatch, fake_client)

    result = await p.synthesize(_req())

    assert result.fallback_used is True
    assert result.audio_b64  # stub still produced a real-duration WAV


async def test_sagemaker_synthesize_falls_back_on_empty_audio(monkeypatch):
    fake_client = _FakeSageMakerClient(response={"Body": _FakeBody(b""), "ContentType": "audio/wav"})
    p = _make_provider(monkeypatch, fake_client)

    result = await p.synthesize(_req())

    assert result.fallback_used is True
    assert result.audio_b64


async def test_sagemaker_stream_yields_single_final_chunk(monkeypatch):
    wav = _wav()
    fake_client = _FakeSageMakerClient(response={"Body": _FakeBody(wav), "ContentType": "audio/wav"})
    p = _make_provider(monkeypatch, fake_client)

    chunks = [c async for c in p.stream(_req())]

    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].seq == 0
    assert chunks[0].audio_b64 == base64.b64encode(wav).decode()
    assert chunks[0].sample_rate == CANONICAL_SAMPLE_RATE


async def test_sagemaker_stream_falls_back_and_still_yields_one_chunk(monkeypatch):
    fake_client = _FakeSageMakerClient(exc=RuntimeError("down"))
    p = _make_provider(monkeypatch, fake_client)

    chunks = [c async for c in p.stream(_req())]

    assert len(chunks) == 1
    assert chunks[0].is_final is True
