"""WhisperSageMakerProvider — self-hosted Whisper via boto3 sagemaker-runtime.

All hermetic — NO real AWS call. ``boto3.client`` is monkeypatched to a fake
sagemaker-runtime client that records ``invoke_endpoint`` calls and replays a
scripted response, so these tests pin the SDK mechanics (client built once,
correct ``invoke_endpoint`` call, response body parsed) and the mapping into
``TranscriptResult``.

The response JSON shape (``{"text", "language", "confidence"}``) asserted here
is the provider's ASSUMED self-hosted-Whisper convention (see the class
docstring in ``koel/stt/providers.py``) — there is no live SageMaker endpoint
yet to verify it against (Terraform for that infra is written but not
applied). These tests pin *this* code's behavior against *that* assumption,
not a verified vendor contract.
"""

from __future__ import annotations

import base64
import json

import boto3
import pytest

from koel.common.schemas import Language
from koel.stt.providers import WhisperSageMakerProvider
from koel.stt.schemas import TranscribeRequest

# --- Fakes ---------------------------------------------------------------


class _FakeBody:
    """Stand-in for botocore's StreamingBody — only `.read()` is used."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeSageMakerRuntimeClient:
    """Fake sagemaker-runtime client: records calls, replays a scripted response."""

    def __init__(self, response_payload: bytes | None = None, exc: Exception | None = None) -> None:
        self._response_payload = response_payload
        self._exc = exc
        self.calls: list[dict] = []

    def invoke_endpoint(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return {"Body": _FakeBody(self._response_payload)}


def _make_provider(monkeypatch, *, response: dict | None = None, exc: Exception | None = None):
    payload = json.dumps(response).encode() if response is not None else None
    fake_client = _FakeSageMakerRuntimeClient(response_payload=payload, exc=exc)
    captured: dict = {}

    def fake_boto3_client(service_name, **kwargs):  # noqa: ANN001, ANN003
        captured["service_name"] = service_name
        captured["kwargs"] = kwargs
        return fake_client

    monkeypatch.setattr(boto3, "client", fake_boto3_client)
    provider = WhisperSageMakerProvider(endpoint_name="whisper-endpoint", region="ap-south-1")
    return provider, fake_client, captured


def _req(**kw) -> TranscribeRequest:  # noqa: ANN003
    base = dict(
        session_id="s1",
        audio_b64=base64.b64encode(b"\x00\x01" * 400).decode(),
        is_final=True,
        audio_format="wav",
    )
    return TranscribeRequest(**{**base, **kw})


# --- 1. Client construction: built once in __init__, correct service/region ---


def test_init_builds_sagemaker_runtime_client_once(monkeypatch):
    _, _, captured = _make_provider(monkeypatch, response={"text": "hi"})

    assert captured["service_name"] == "sagemaker-runtime"
    assert captured["kwargs"] == {"region_name": "ap-south-1"}


# --- 2. Successful transcribe → correctly mapped TranscriptResult -------------


async def test_successful_transcribe_maps_to_transcript_result(monkeypatch):
    provider, fake_client, _ = _make_provider(
        monkeypatch, response={"text": "mera balance kya hai", "language": "hindi", "confidence": 0.87}
    )

    result = await provider.transcribe(_req())

    assert result.session_id == "s1"
    assert result.text == "mera balance kya hai"
    assert result.is_final is True
    assert result.language_id == Language.hi
    assert result.language_confidence == 0.87
    assert result.word_confidence == 0.87
    # No `duration` in the response → estimated like GroqWhisperProvider's no-duration case.
    assert result.speech_duration_ms == max(120, len(result.text) * 60)

    # Exactly one invoke_endpoint call, with the expected endpoint + content type.
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["EndpointName"] == "whisper-endpoint"
    assert call["ContentType"] == "audio/wav"
    assert call["Body"] == base64.b64decode(_req().audio_b64)


async def test_non_wav_audio_uses_octet_stream_content_type(monkeypatch):
    provider, fake_client, _ = _make_provider(monkeypatch, response={"text": "hello"})

    await provider.transcribe(_req(audio_format="m4a"))

    assert fake_client.calls[0]["ContentType"] == "application/octet-stream"


async def test_duration_field_is_honored_when_present(monkeypatch):
    provider, _, _ = _make_provider(
        monkeypatch, response={"text": "hello there", "language": "english", "duration": 2.5}
    )

    result = await provider.transcribe(_req())

    assert result.speech_duration_ms == 2500


async def test_confidence_defaults_to_point_nine_when_absent(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, response={"text": "hello", "language": "english"})

    result = await provider.transcribe(_req())

    assert result.language_confidence == 0.9
    assert result.word_confidence == 0.9


async def test_language_hint_used_when_response_language_unrecognized(monkeypatch):
    provider, _, _ = _make_provider(
        monkeypatch, response={"text": "haan theek hai", "language": "marathi"}
    )

    result = await provider.transcribe(_req(language_hint=Language.hinglish))

    assert result.language_id == Language.hinglish  # hint wins over an unmapped language


async def test_language_defaults_to_other_without_hint_or_recognized_language(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, response={"text": "hello"})

    result = await provider.transcribe(_req())

    assert result.language_id == Language.other


# --- 3. Failures propagate — never silently swallowed/degraded ---------------


async def test_invoke_endpoint_exception_propagates(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, exc=ConnectionError("endpoint unreachable"))

    with pytest.raises(RuntimeError, match="invoke_endpoint failed"):
        await provider.transcribe(_req())


async def test_unexpected_response_shape_raises(monkeypatch):
    # Missing the assumed 'text' field entirely — must raise, not silently coerce.
    provider, _, _ = _make_provider(monkeypatch, response={"unexpected": "shape"})

    with pytest.raises(RuntimeError, match="unexpected SageMaker response shape"):
        await provider.transcribe(_req())


async def test_invalid_base64_audio_raises_value_error(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, response={"text": "hi"})

    with pytest.raises(ValueError, match="invalid base64"):
        await provider.transcribe(_req(audio_b64="not-valid-base64!!"))


async def test_empty_audio_raises_value_error(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, response={"text": "hi"})

    with pytest.raises(ValueError, match="empty audio payload"):
        await provider.transcribe(_req(audio_b64=""))


# --- 4. warm()/aclose() are harmless no-ops -----------------------------------


async def test_warm_and_aclose_are_noops(monkeypatch):
    provider, _, _ = _make_provider(monkeypatch, response={"text": "hi"})

    assert await provider.warm() is None
    assert await provider.aclose() is None
