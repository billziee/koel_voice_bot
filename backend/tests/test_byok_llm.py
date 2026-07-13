"""BYOK LLM connection wiring in NLP_svc (Phase 2): _provider_for resolves a
per-bot custom connection into an OpenAI-compatible provider, and degrades to the
service default whenever the connection is absent / unresolvable."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from koel.common import crypto
from koel.common.model_connections import create_connection, deactivate_connection
from koel.common.schemas import Language
from koel.nlp import main as nlp_main
from koel.nlp.providers import GroqNlpProvider
from koel.nlp.schemas import TurnContext


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    nlp_main._conn_providers.clear()  # noqa: SLF001 — isolate the module cache per test
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001
    nlp_main._conn_providers.clear()  # noqa: SLF001


def _ctx(conn_id: str | None) -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="collections", language=Language.en,
        customer_text="hi", org_id="orgA", llm_connection_id=conn_id,
    )


async def test_no_connection_uses_the_default_provider(test_db):
    provider = await nlp_main._provider_for(_ctx(None))  # noqa: SLF001
    assert provider is nlp_main._provider  # noqa: SLF001


async def test_custom_connection_builds_openai_compatible_provider(test_db):
    # Literal public IP → SSRF guard passes without a DNS lookup (hermetic).
    conn = await create_connection(
        org_id="orgA", name="my-openai", kind="llm", provider="openai_compatible",
        base_url="https://8.8.8.8/v1", api_key="sk_custom", model="gpt-x-mini",
        params={"temperature": 0.7, "max_tokens": 99},
    )
    provider = await nlp_main._provider_for(_ctx(conn["id"]))  # noqa: SLF001
    assert isinstance(provider, GroqNlpProvider)
    assert provider is not nlp_main._provider  # noqa: SLF001
    assert provider._url == "https://8.8.8.8/v1/chat/completions"  # noqa: SLF001
    assert provider._model == "gpt-x-mini"  # noqa: SLF001
    assert provider._temperature == 0.7  # noqa: SLF001
    assert provider._max_tokens == 99  # noqa: SLF001
    # cached: same id → same instance
    again = await nlp_main._provider_for(_ctx(conn["id"]))  # noqa: SLF001
    assert again is provider


async def test_unknown_or_inactive_connection_degrades_to_default(test_db):
    assert await nlp_main._provider_for(_ctx("nope")) is nlp_main._provider  # noqa: SLF001
    conn = await create_connection(org_id="orgA", name="x", kind="llm", api_key="k", model="m")
    await deactivate_connection(conn["id"], "orgA")
    assert await nlp_main._provider_for(_ctx(conn["id"])) is nlp_main._provider  # noqa: SLF001


async def test_wrong_kind_connection_is_ignored(test_db):
    conn = await create_connection(org_id="orgA", name="a-tts", kind="tts", api_key="k", model="voice")
    assert await nlp_main._provider_for(_ctx(conn["id"])) is nlp_main._provider  # noqa: SLF001
