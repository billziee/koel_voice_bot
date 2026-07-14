"""India-hosted NLP via Sarvam chat completions (O-7 Track A)."""

from __future__ import annotations

import json

from koel.common.schemas import Language
from koel.nlp.providers import SarvamNlpProvider
from koel.nlp.schemas import Intent, TurnContext


def _ctx() -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="collections", language=Language.hinglish,
        customer_text="theek hai", history=[], crm_context={},
    )


def test_sarvam_uses_v1_endpoint_and_subscription_key_auth():
    p = SarvamNlpProvider(api_key="sk_test", model="sarvam-m")
    assert p._url == "https://api.sarvam.ai/v1/chat/completions"
    assert p._headers["api-subscription-key"] == "sk_test"
    assert "Authorization" not in p._headers  # NOT Bearer, unlike Groq


def test_build_provider_selects_sarvam_when_configured(monkeypatch):
    from koel.nlp import config, main

    def _settings():
        s = config.NlpSettings()
        s.nlp_provider = "sarvam"
        s.sarvam_api_key = "sk_test"
        return s

    monkeypatch.setattr(main, "get_nlp_settings", _settings)
    assert isinstance(main._build_provider(), SarvamNlpProvider)


def test_build_provider_falls_back_to_stub_without_a_key(monkeypatch):
    from koel.nlp import config, main
    from koel.nlp.providers import StubNlpProvider

    def _settings():
        s = config.NlpSettings()
        s.nlp_provider = "sarvam"  # selected but no key → not enabled
        s.sarvam_api_key = None
        return s

    monkeypatch.setattr(main, "get_nlp_settings", _settings)
    assert isinstance(main._build_provider(), StubNlpProvider)


async def test_generate_reuses_the_groq_json_contract(monkeypatch):
    # Sarvam's chat API is OpenAI-shaped, so the inherited parse path works: a
    # canned completion round-trips into a validated NlpResponse.
    p = SarvamNlpProvider(api_key="sk_test", model="sarvam-m")
    content = json.dumps({
        "response_text": "Bilkul, main aapki madad karti hoon.",
        "intent": "interested",
        "tone": "empathetic",
    })
    body = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}}

    async def fake_post(url, json=None, headers=None):  # noqa: ANN001
        assert url == "https://api.sarvam.ai/v1/chat/completions"
        assert headers["api-subscription-key"] == "sk_test"

        class _R:
            def raise_for_status(self) -> None: ...
            def json(self) -> dict:
                return body
        return _R()

    monkeypatch.setattr(p._client, "post", fake_post)
    resp = await p.generate(_ctx(), [])
    assert resp.intent is Intent.interested
    assert "madad" in resp.response_text
    await p.aclose()
