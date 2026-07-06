"""Sarvam transliteration client used to derive Hinglish copy at startup."""

from __future__ import annotations

import httpx
import pytest

from koel.voicebot.pipeline.transliteration import SarvamTransliterator


async def test_to_hinglish_returns_transliterated_text(monkeypatch):
    p = SarvamTransliterator(api_key="k")
    captured: dict = {}

    async def ok(url, headers=None, json=None):  # noqa: ANN001
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        payload = {"request_id": "r1", "transliterated_text": "Namaste ji"}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", ok)
    result = await p.to_hinglish("नमस्ते जी")

    assert captured["url"].endswith("/transliterate")
    assert captured["headers"]["api-subscription-key"] == "k"
    assert captured["json"] == {
        "input": "नमस्ते जी", "source_language_code": "hi-IN", "target_language_code": "en-IN",
    }
    assert result == "Namaste ji"
    await p.aclose()


async def test_to_hinglish_raises_on_empty_response(monkeypatch):
    p = SarvamTransliterator(api_key="k")

    async def empty(url, headers=None, json=None):  # noqa: ANN001
        payload = {"request_id": "r1", "transliterated_text": ""}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(p._client, "post", empty)
    with pytest.raises(ValueError, match="empty"):
        await p.to_hinglish("नमस्ते")
    await p.aclose()


async def test_to_hinglish_raises_on_unreachable_host():
    p = SarvamTransliterator(api_key="k", base_url="http://127.0.0.1:1", timeout_s=2)
    with pytest.raises(httpx.HTTPError):
        await p.to_hinglish("नमस्ते")
    await p.aclose()
