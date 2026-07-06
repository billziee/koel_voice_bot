"""ServiceClients HTTP fan-out via a mock transport (no live services)."""

from __future__ import annotations

import httpx
import pytest

from koel.common.config import CommonSettings
from koel.nlp.schemas import TurnContext
from koel.stt.schemas import TranscribeRequest
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.clients import ServiceClients


def _clients(handler) -> ServiceClients:
    c = ServiceClients(CommonSettings())
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def test_headers_carry_service_bearer_and_trace():
    headers = ServiceClients(CommonSettings())._headers()
    assert headers["Authorization"].startswith("Bearer ")


async def test_transcribe_respond_synthesize_roundtrip():
    def handler(request: httpx.Request) -> httpx.Response:
        # The service-principal bearer must be present on every hop.
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path.endswith("/transcribe"):
            return httpx.Response(200, json={
                "session_id": "s", "text": "haan", "is_final": True,
                "language_id": "hinglish", "language_confidence": 0.9,
                "speech_duration_ms": 500, "word_confidence": 0.9,
            })
        if request.url.path.endswith("/respond"):
            return httpx.Response(200, json={
                "session_id": "s", "response_text": "great", "intent": "interested",
                "tone": "cheerful", "confidence": 0.9,
            })
        if request.url.path.endswith("/synthesize"):
            return httpx.Response(200, json={
                "session_id": "s", "audio_b64": "AAA=", "duration_ms": 100,
                "prosody": {"rate": 1.0, "pitch": 0.0, "style": "cheerful"},
            })
        return httpx.Response(404)

    c = _clients(handler)
    stt = await c.transcribe(TranscribeRequest(session_id="s", audio_b64="x", is_final=True))
    assert stt.text == "haan"
    nlp = await c.respond(TurnContext(session_id="s", bot_id="b", use_case="credit_card",
                                      language="hinglish", customer_text="haan"))
    assert nlp.response_text == "great"
    tts = await c.synthesize(SynthesizeRequest(session_id="s", text="hi"))
    assert tts.duration_ms == 100
    await c.aclose()


async def test_flag_and_log_interaction_post_to_crm():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(201, json={"status": "ok"})

    c = _clients(handler)
    await c.flag_customer("cust_1", "opt_out", "requested")
    assert any(p.endswith("/flags") for p in seen)
    await c.aclose()


async def test_add_call_summary_posts_to_crm():
    import json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"status": "logged"})

    c = _clients(handler)
    await c.add_call_summary(
        "cust_1", session_id="s1", interest_shown=True,
        objection="too expensive", sentiment="negative", next_action="close",
    )
    assert captured["path"].endswith("/call-summary")
    assert captured["body"] == {
        "session_id": "s1", "interest_shown": True,
        "objection": "too expensive", "sentiment": "negative", "next_action": "close",
    }
    await c.aclose()


async def test_stream_synthesis_yields_ndjson_chunks():
    def handler(request: httpx.Request) -> httpx.Response:
        lines = "\n".join(
            f'{{"session_id":"s","seq":{i},"audio_b64":"AA==","is_final":{str(i == 1).lower()}}}'
            for i in range(2)
        )
        return httpx.Response(200, text=lines + "\n")

    c = _clients(handler)
    chunks = [ch async for ch in c.stream_synthesis(SynthesizeRequest(session_id="s", text="hi"))]
    assert len(chunks) == 2 and chunks[-1].is_final is True
    await c.aclose()


async def test_raise_for_status_propagates_on_5xx():
    c = _clients(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await c.synthesize(SynthesizeRequest(session_id="s", text="hi"))
    await c.aclose()
