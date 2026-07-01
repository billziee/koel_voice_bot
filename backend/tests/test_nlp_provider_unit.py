"""GroqNlpProvider internals: parse, usage, retriability, degrade path."""

from __future__ import annotations

import json

import httpx
import pytest

from koel.common.schemas import Language, Tone
from koel.nlp.providers import GroqNlpProvider
from koel.nlp.schemas import Intent, RetrievedChunk, TurnContext

CHUNKS = [RetrievedChunk(title="Objection: annual fee", content="Pehle saal fee waived.", score=0.8)]


def _ctx(text="annual fee kitni hai") -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish,
        customer_text=text, crm_context={"name": "Rahul"}, rag_index="cc_hdfc",
    )


def _provider() -> GroqNlpProvider:
    return GroqNlpProvider(api_key="k", model="llama-3.3-70b-versatile")


def test_parse_valid_json_maps_all_fields():
    raw = json.dumps({
        "response_text": "Pehle saal fee waived hai.", "intent": "objection", "tone": "empathetic",
        "escalate": False, "end_call": False, "confidence": 0.9,
        "citations": [{"title": "Objection: annual fee", "excerpt": "waived"}],
    })
    r = _provider()._parse(_ctx(), CHUNKS, raw)
    assert r.intent == Intent.objection and r.tone == Tone.empathetic
    assert r.grounded is True and [c.title for c in r.citations] == ["Objection: annual fee"]


def test_parse_drops_hallucinated_citation_and_downgrades():
    raw = json.dumps({
        "response_text": "Aapko 10% cashback milega.", "intent": "interested", "tone": "cheerful",
        "confidence": 0.95, "citations": [{"title": "Invented Chunk", "excerpt": "x"}],
    })
    r = _provider()._parse(_ctx(), CHUNKS, raw)
    assert r.citations == []  # not in retrieved set → dropped
    assert r.grounded is False and r.confidence <= 0.4  # unbacked product claim


def test_parse_coerces_bad_enums_and_clamps_confidence():
    raw = json.dumps({"response_text": "ok", "intent": "???", "tone": "???", "confidence": 5})
    r = _provider()._parse(_ctx(), [], raw)
    assert r.intent == Intent.unknown
    assert r.tone in Tone
    assert 0.0 <= r.confidence <= 1.0


def test_parse_empty_response_text_raises():
    with pytest.raises(ValueError):
        _provider()._parse(_ctx(), [], json.dumps({"response_text": "  ", "intent": "interested"}))


def test_retriable_classification():
    p = _provider()
    assert p._is_retriable(httpx.TimeoutException("t")) is True
    resp429 = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    assert p._is_retriable(httpx.HTTPStatusError("e", request=resp429.request, response=resp429)) is True
    resp400 = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    assert p._is_retriable(httpx.HTTPStatusError("e", request=resp400.request, response=resp400)) is False
    assert p._is_retriable(ValueError("x")) is False


def test_record_usage_is_safe_on_junk():
    p = _provider()
    p._record_usage({"total_tokens": "not-a-number"})  # must not raise
    p._record_usage({})  # no tokens → no-op


async def test_generate_degrades_to_stub_on_http_error(monkeypatch):
    p = _provider()

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(p._client, "post", boom)
    r = await p.generate(_ctx("mujhe agent se baat karni hai"), CHUNKS)
    # Never drops the turn — falls back to the deterministic stub.
    assert r.response_text and r.intent == Intent.request_human
    await p.aclose()


async def test_generate_parses_live_style_response(monkeypatch):
    p = _provider()
    body = {
        "choices": [{"message": {"content": json.dumps({
            "response_text": "Bilkul, fee pehle saal waived hai.", "intent": "objection",
            "tone": "empathetic", "confidence": 0.88,
            "citations": [{"title": "Objection: annual fee", "excerpt": "waived"}],
        })}}],
        "usage": {"total_tokens": 128},
    }

    async def ok(*a, **k):
        return httpx.Response(200, json=body, request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(p._client, "post", ok)
    r = await p.generate(_ctx(), CHUNKS)
    assert r.intent == Intent.objection and r.grounded is True
    await p.aclose()
