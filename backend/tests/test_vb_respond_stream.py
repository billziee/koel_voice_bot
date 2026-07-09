"""VB_Svc client for NLP first-token streaming (roadmap O-2): consuming the
NLP_svc /v1/respond/stream NDJSON into typed frames."""

from __future__ import annotations

import httpx

from koel.common.config import CommonSettings
from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse, NlpStreamFrame, TurnContext
from koel.voicebot.clients import ServiceClients


def _ctx() -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="credit_card", language=Language.hinglish,
        customer_text="haan bataiye", crm_context={"name": "Rahul"}, rag_index="cc_hdfc",
    )


def _ndjson_clients(frames: list[NlpStreamFrame]) -> ServiceClients:
    body = "".join(f.model_dump_json() + "\n" for f in frames)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    c = ServiceClients(CommonSettings())
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_respond_stream_yields_sentence_frames_then_final():
    text = "Namaste ji. Aap kaise hain?"
    frames = [
        NlpStreamFrame(type="sentence", text="Namaste ji."),
        NlpStreamFrame(type="sentence", text="Aap kaise hain?"),
        NlpStreamFrame(
            type="final",
            response=NlpResponse(
                session_id="s", response_text=text, intent=Intent.interested, tone=Tone.cheerful
            ),
        ),
    ]
    c = _ndjson_clients(frames)

    got = [f async for f in c.respond_stream(_ctx())]
    assert [f.type for f in got] == ["sentence", "sentence", "final"]
    assert [f.text for f in got if f.type == "sentence"] == ["Namaste ji.", "Aap kaise hain?"]
    final = got[-1]
    assert final.response is not None
    assert final.response.response_text == text
    assert final.response.intent == Intent.interested
    await c.aclose()


async def test_respond_stream_ignores_blank_lines():
    frames = [NlpStreamFrame(type="final", response=NlpResponse(
        session_id="s", response_text="ok.", intent=Intent.unknown, tone=Tone.neutral))]
    body = "\n" + frames[0].model_dump_json() + "\n\n"  # stray blank lines around the frame

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    c = ServiceClients(CommonSettings())
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = [f async for f in c.respond_stream(_ctx())]
    assert len(got) == 1 and got[0].type == "final"
    await c.aclose()
