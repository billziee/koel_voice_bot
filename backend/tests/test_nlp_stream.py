"""GroqNlpProvider.generate_stream (roadmap O-2): first-token sentence streaming,
the terminal full-parse frame, and the fail-safe batch fallback."""

from __future__ import annotations

import json

import httpx

from koel.common.schemas import Language
from koel.common.text import split_sentences
from koel.nlp.providers import GroqNlpProvider, StubNlpProvider
from koel.nlp.schemas import Intent, TurnContext


def _ctx(text: str = "annual fee kitni hai") -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish,
        customer_text=text, crm_context={"name": "Rahul"}, rag_index="cc_hdfc",
    )


def _sse(pieces: list[str], *, usage_tokens: int = 40) -> str:
    """A Groq SSE body: one delta per piece, then a usage chunk, then [DONE]."""
    lines = [json.dumps({"choices": [{"delta": {"content": p}}]}) for p in pieces]
    lines.append(json.dumps({"choices": [], "usage": {"total_tokens": usage_tokens}}))
    return "".join(f"data: {ln}\n\n" for ln in lines) + "data: [DONE]\n\n"


def _streaming_provider(sse_body: str) -> GroqNlpProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body)

    p = GroqNlpProvider(api_key="k", model="llama-3.3-70b-versatile")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


async def test_generate_stream_emits_sentences_then_final():
    text = "Namaste ji. Aap kaise hain? Main theek hoon."
    raw = json.dumps({"intent": "interested", "response_text": text, "tone": "cheerful"})
    pieces = [raw[i : i + 5] for i in range(0, len(raw), 5)]  # token-ish streaming
    p = _streaming_provider(_sse(pieces))

    frames = [f async for f in p.generate_stream(_ctx(), [])]
    sentences = [f.text for f in frames if f.type == "sentence"]
    finals = [f for f in frames if f.type == "final"]

    assert sentences == split_sentences(text)  # streamed sentences == batch split
    assert len(finals) == 1
    assert finals[0].response is not None
    assert finals[0].response.response_text == text  # final never diverges
    assert finals[0].response.intent == Intent.interested
    await p._client.aclose()


async def test_generate_stream_falls_back_to_a_real_turn_on_stream_error():
    # 500 before any content → clean fallback (batch generate → degrades to the
    # stub, since the same transport 500s) — the turn is never dropped.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    p = GroqNlpProvider(api_key="k", model="m")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    frames = [f async for f in p.generate_stream(_ctx(), [])]
    finals = [f for f in frames if f.type == "final"]
    assert len(finals) == 1
    assert finals[0].response is not None and finals[0].response.response_text
    await p._client.aclose()


async def test_stub_generate_stream_default_emits_sentences_then_final():
    frames = [f async for f in StubNlpProvider().generate_stream(_ctx(), [])]
    sentences = [f.text for f in frames if f.type == "sentence"]
    finals = [f for f in frames if f.type == "final"]
    assert len(finals) == 1 and finals[0].response is not None
    # The default (non-streaming) path still splits the reply into sentence frames.
    assert sentences == split_sentences(finals[0].response.response_text)


async def test_respond_stream_route_always_emits_a_terminal_final(monkeypatch):
    # Review #6: even if generate_stream raises mid-stream, the /v1/respond/stream
    # route must still emit a terminal `final` frame (VB_Svc's contract), and mark
    # it escalate so a degraded turn hands off rather than continuing on unknown.
    from koel.nlp import main as nlp_main
    from koel.nlp.schemas import NlpStreamFrame

    class _BoomProvider:
        async def generate_stream(self, ctx, retrieved):  # noqa: ANN001
            yield NlpStreamFrame(type="sentence", text="Namaste ji.")
            raise RuntimeError("groq stream died before final")

    monkeypatch.setattr(nlp_main, "_provider", _BoomProvider())
    ctx = TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish,
        customer_text="haan", rag_enabled=False,  # no RAG → no DB in _retrieve_for
    )
    resp = await nlp_main.respond_stream(ctx, None)  # principal unused by the body
    body = "".join([chunk async for chunk in resp.body_iterator])
    frames = [NlpStreamFrame.model_validate_json(ln) for ln in body.splitlines() if ln.strip()]

    assert frames[-1].type == "final"  # terminal frame guaranteed despite the raise
    assert frames[-1].response is not None and frames[-1].response.escalate is True
