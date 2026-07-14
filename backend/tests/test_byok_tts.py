"""BYOK TTS connection wiring in TTS_svc (Phase 3): _provider_for resolves a
per-bot custom connection into a provider chosen by its ``provider`` label
(deepgram → Aura, openai_compatible → /audio/speech, sarvam → Bulbul), and
degrades to the service default whenever the connection is absent, wrong-kind,
an unknown label, or otherwise unresolvable.

Hermetic — NO live keys. Connections are created via
``koel.common.model_connections.create_connection`` with KOEL_SECRET_KEY set and
the crypto cache cleared; the ``base_url`` is a literal public IP so the SSRF
guard passes without a DNS lookup. The outbound HTTP call is monkeypatched to
assert the provider posts to the right URL with the key in the header (and never
logs it).
"""

from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from koel.common import crypto
from koel.common.model_connections import create_connection, deactivate_connection
from koel.tts import main as tts_main
from koel.tts.providers import (
    DeepgramTtsProvider,
    OpenAiCompatibleTtsProvider,
    SarvamTtsProvider,
    _pcm_to_wav,
)
from koel.tts.schemas import SynthesizeRequest


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    tts_main._conn_providers.clear()  # noqa: SLF001 — isolate the module cache per test
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001
    tts_main._conn_providers.clear()  # noqa: SLF001


def _req(conn_id: str | None, text: str = "hello there") -> SynthesizeRequest:
    return SynthesizeRequest(session_id="s", text=text, tts_connection_id=conn_id)


class _FakeResp:
    def __init__(self, content: bytes, content_type: str = "audio/wav") -> None:
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        return None


# --- default / no-connection path is unchanged --------------------------------


async def test_no_connection_uses_the_default_provider(test_db):
    provider = await tts_main._provider_for(_req(None))  # noqa: SLF001
    assert provider is tts_main._provider  # noqa: SLF001


# --- provider label → concrete provider ---------------------------------------


async def test_deepgram_connection_builds_aura_provider(test_db):
    conn = await create_connection(
        org_id="orgA", name="my-aura", kind="tts", provider="deepgram",
        base_url="https://8.8.8.8", api_key="dg_key", model="aura-2-thalia-en",
        params={"voice": "aura-2-thalia-en", "sample_rate": 24000},
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, DeepgramTtsProvider)
    assert provider is not tts_main._provider  # noqa: SLF001
    assert provider._url == "https://8.8.8.8/v1/speak"  # noqa: SLF001
    assert provider._fallback is tts_main._provider  # noqa: SLF001 — degrades to default
    # cached: same id → same instance (no rebuild without an edit)
    again = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert again is provider


async def test_openai_compatible_connection_builds_provider(test_db):
    conn = await create_connection(
        org_id="orgA", name="my-openai-tts", kind="tts", provider="openai_compatible",
        base_url="https://8.8.8.8/v1", api_key="sk_tts", model="tts-1",
        params={"voice": "nova"},
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, OpenAiCompatibleTtsProvider)
    assert provider._url == "https://8.8.8.8/v1/audio/speech"  # noqa: SLF001
    assert provider._voice == "nova"  # noqa: SLF001
    assert provider._model == "tts-1"  # noqa: SLF001


async def test_sarvam_connection_builds_sarvam_provider(test_db):
    conn = await create_connection(
        org_id="orgA", name="my-bulbul", kind="tts", provider="sarvam",
        base_url="https://8.8.8.8", api_key="sk_sarvam", model="bulbul:v3",
        params={"voice": "priya", "sample_rate": 24000},
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, SarvamTtsProvider)
    assert provider._url == "https://8.8.8.8/text-to-speech"  # noqa: SLF001


# --- degrade chain: absent / inactive / wrong-kind / unknown label → default --


async def test_unknown_or_inactive_connection_degrades_to_default(test_db):
    assert await tts_main._provider_for(_req("nope")) is tts_main._provider  # noqa: SLF001
    conn = await create_connection(
        org_id="orgA", name="x", kind="tts", provider="deepgram", api_key="k", model="m"
    )
    await deactivate_connection(conn["id"], "orgA")
    assert await tts_main._provider_for(_req(conn["id"])) is tts_main._provider  # noqa: SLF001


async def test_wrong_kind_connection_is_ignored(test_db):
    conn = await create_connection(
        org_id="orgA", name="an-llm", kind="llm", api_key="k", model="m"
    )
    assert await tts_main._provider_for(_req(conn["id"])) is tts_main._provider  # noqa: SLF001


async def test_unknown_provider_label_degrades_to_default(test_db):
    conn = await create_connection(
        org_id="orgA", name="mystery", kind="tts", provider="acme-labs",
        base_url="https://8.8.8.8", api_key="k", model="m",
    )
    assert await tts_main._provider_for(_req(conn["id"])) is tts_main._provider  # noqa: SLF001


# --- outbound contract: right URL + key in the header, never logged -----------


async def test_deepgram_posts_to_speak_with_token_header(test_db, monkeypatch, caplog):
    conn = await create_connection(
        org_id="orgA", name="aura", kind="tts", provider="deepgram",
        base_url="https://8.8.8.8", api_key="dg_secret", model="aura-2-thalia-en",
        params={"sample_rate": 24000},
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, DeepgramTtsProvider)

    captured: dict = {}

    async def fake_post(url, *, headers=None, params=None, json=None):  # noqa: ANN001, ANN003
        captured.update(url=url, headers=headers, params=params, json=json)
        return _FakeResp(b"\x00\x00" * 2400)  # 0.1s of 24kHz 16-bit PCM

    monkeypatch.setattr(provider._client, "post", fake_post)  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        result = await provider.synthesize(_req(conn["id"], text="pay your bill"))

    assert captured["url"] == "https://8.8.8.8/v1/speak"
    assert captured["headers"]["Authorization"] == "Token dg_secret"
    assert captured["params"]["model"] == "aura-2-thalia-en"
    assert captured["params"]["encoding"] == "linear16"
    assert captured["params"]["sample_rate"] == 24000
    assert captured["json"] == {"text": "pay your bill"}
    assert result.sample_rate == 24000
    assert result.duration_ms == 100  # exact from the PCM length
    assert not result.fallback_used
    # The key is confined to the request header and never reaches a log line.
    assert "dg_secret" not in caplog.text


async def test_openai_compatible_posts_bearer_to_audio_speech(test_db, monkeypatch, caplog):
    conn = await create_connection(
        org_id="orgA", name="oai", kind="tts", provider="openai_compatible",
        base_url="https://8.8.8.8/v1", api_key="sk_secret", model="tts-1",
        params={"voice": "shimmer"},
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, OpenAiCompatibleTtsProvider)

    captured: dict = {}
    wav = _pcm_to_wav(b"\x00\x00" * 2400, 24000)

    async def fake_post(url, *, headers=None, json=None):  # noqa: ANN001, ANN003
        captured.update(url=url, headers=headers, json=json)
        return _FakeResp(wav)

    monkeypatch.setattr(provider._client, "post", fake_post)  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        result = await provider.synthesize(_req(conn["id"], text="namaste"))

    assert captured["url"] == "https://8.8.8.8/v1/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer sk_secret"
    assert captured["json"]["model"] == "tts-1"
    assert captured["json"]["input"] == "namaste"
    assert captured["json"]["voice"] == "shimmer"
    assert result.sample_rate == 24000
    assert not result.fallback_used
    assert "sk_secret" not in caplog.text


async def test_deepgram_error_degrades_to_fallback(test_db, monkeypatch):
    conn = await create_connection(
        org_id="orgA", name="aura", kind="tts", provider="deepgram",
        base_url="https://8.8.8.8", api_key="dg_secret", model="aura-2-thalia-en",
    )
    provider = await tts_main._provider_for(_req(conn["id"]))  # noqa: SLF001

    async def boom(*a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("deepgram unreachable")

    monkeypatch.setattr(provider._client, "post", boom)  # noqa: SLF001

    result = await provider.synthesize(_req(conn["id"], text="fallback please"))
    # Degraded to the wrapped default provider — a clip is still produced.
    assert result.fallback_used is True
    assert result.audio_b64
