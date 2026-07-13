"""BYOK STT connection wiring in STT_svc (Phase 3): _provider_for resolves a
per-bot custom connection into a provider chosen by its ``provider`` label
(deepgram → nova-3 realtime, sarvam → Saaras), and degrades to the service
default whenever the connection is absent, wrong-kind, an unknown label, or
otherwise unresolvable. The connection wins over the env-default streaming
provider (O-6) when set — the built provider's WS seam is armed.

Hermetic — NO live keys. Connections are created via
``koel.common.model_connections.create_connection`` with KOEL_SECRET_KEY set and
the crypto cache cleared; ``base_url`` is a literal public IP so the SSRF guard
passes without a DNS lookup.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from koel.common import crypto
from koel.common.model_connections import create_connection, deactivate_connection
from koel.stt import main as stt_main
from koel.stt.providers import DeepgramSttProvider, SarvamSttProvider
from koel.stt.schemas import TranscribeRequest


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    stt_main._conn_providers.clear()  # noqa: SLF001 — isolate the module cache per test
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001
    stt_main._conn_providers.clear()  # noqa: SLF001


def _req(conn_id: str | None) -> TranscribeRequest:
    return TranscribeRequest(
        session_id="s", audio_b64="", is_final=True, stt_connection_id=conn_id
    )


# --- default / no-connection path is unchanged --------------------------------


async def test_no_connection_uses_the_default_provider(test_db):
    provider = await stt_main._provider_for(_req(None))  # noqa: SLF001
    assert provider is stt_main._provider  # noqa: SLF001


# --- provider label → concrete provider ---------------------------------------


async def test_deepgram_connection_builds_deepgram_provider(test_db):
    conn = await create_connection(
        org_id="orgA", name="my-nova", kind="stt", provider="deepgram",
        base_url="https://8.8.8.8", api_key="dg_key", model="nova-3",
    )
    provider = await stt_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, DeepgramSttProvider)
    assert provider is not stt_main._provider  # noqa: SLF001
    assert provider._model == "nova-3"  # noqa: SLF001
    assert provider._api_key == "dg_key"  # noqa: SLF001
    assert provider._ws_base == "wss://8.8.8.8"  # noqa: SLF001
    assert provider._fallback is stt_main._provider  # noqa: SLF001 — batch delegates to default
    # cached: same id → same instance (no rebuild without an edit)
    again = await stt_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert again is provider


async def test_sarvam_connection_builds_sarvam_provider_with_ws_armed(test_db):
    conn = await create_connection(
        org_id="orgA", name="my-saaras", kind="stt", provider="sarvam",
        base_url="https://8.8.8.8", api_key="sk_sarvam", model="saaras:v3",
        params={"mode": "transcribe"},
    )
    provider = await stt_main._provider_for(_req(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, SarvamSttProvider)
    assert provider._url == "https://8.8.8.8/speech-to-text"  # noqa: SLF001
    assert provider._model == "saaras:v3"  # noqa: SLF001
    assert provider._stream_stt is True  # noqa: SLF001 — connection wins → WS seam armed
    assert provider._fallback is stt_main._provider  # noqa: SLF001


# --- degrade chain: absent / inactive / wrong-kind / unknown label → default --


async def test_unknown_or_inactive_connection_degrades_to_default(test_db):
    assert await stt_main._provider_for(_req("nope")) is stt_main._provider  # noqa: SLF001
    conn = await create_connection(
        org_id="orgA", name="x", kind="stt", provider="deepgram", api_key="k", model="m"
    )
    await deactivate_connection(conn["id"], "orgA")
    assert await stt_main._provider_for(_req(conn["id"])) is stt_main._provider  # noqa: SLF001


async def test_wrong_kind_connection_is_ignored(test_db):
    conn = await create_connection(
        org_id="orgA", name="a-tts", kind="tts", provider="deepgram", api_key="k", model="m"
    )
    assert await stt_main._provider_for(_req(conn["id"])) is stt_main._provider  # noqa: SLF001


async def test_unknown_provider_label_degrades_to_default(test_db):
    conn = await create_connection(
        org_id="orgA", name="mystery", kind="stt", provider="acme-labs",
        base_url="https://8.8.8.8", api_key="k", model="m",
    )
    assert await stt_main._provider_for(_req(conn["id"])) is stt_main._provider  # noqa: SLF001
