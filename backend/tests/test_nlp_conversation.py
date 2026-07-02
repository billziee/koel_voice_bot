"""Conversational-quality tests for the stub NLP (deferral, no-pitch, no-repeat)."""

from koel.common.schemas import Language
from koel.nlp.providers import StubNlpProvider
from koel.nlp.schemas import Intent, RetrievedChunk, TurnContext

PROVIDER = StubNlpProvider()
PRODUCT = [
    RetrievedChunk(
        title="Regalia", content="Regalia: airport lounge access, 4x dining rewards.", score=0.9
    )
]


def _ctx(text: str, history=None) -> TurnContext:
    return TurnContext(
        session_id="s",
        bot_id="bot_hdfc_cc",
        use_case="credit_card",
        language=Language.hinglish,
        customer_text=text,
        history=history or [],
        crm_context={"name": "Rahul Sharma"},
        rag_enabled=True,
        rag_index="cc_hdfc",
    )


async def test_busy_offers_callback_and_ends_without_pitching():
    r = await PROVIDER.generate(_ctx("Abhi thoda busy hoon"), PRODUCT)
    assert r.intent == Intent.busy
    assert r.end_call is True
    assert r.escalate is False
    # The bug: it must NOT read the product spec sheet at a busy customer.
    assert "lounge" not in r.response_text.lower()
    assert "regalia" not in r.response_text.lower()
    assert "call" in r.response_text.lower()  # offers a callback


async def test_unknown_asks_a_question_not_a_product_dump():
    r = await PROVIDER.generate(_ctx("mausam accha hai aaj"), PRODUCT)
    assert r.intent == Intent.unknown
    assert r.response_text != PRODUCT[0].content  # not the RAG chunk verbatim
    assert "lounge" not in r.response_text.lower()
    assert "?" in r.response_text  # a clarifying question


async def test_no_back_to_back_repetition():
    # Same bot line already spoken last turn → must not repeat it.
    repeated = "Bilkul samajh sakti hoon. X Interested hain?"
    history = [{"speaker": "bot", "text": repeated}]
    # An objection after one pitch should pivot to the soft WhatsApp close.
    r = await PROVIDER.generate(_ctx("mehnga hai", history=history), PRODUCT)
    assert r.response_text.strip() != repeated.strip()
    assert r.end_call is True


async def test_first_objection_still_handles_normally():
    r = await PROVIDER.generate(_ctx("mere paas already do cards hain"), PRODUCT)
    assert r.intent == Intent.objection
    assert r.end_call is False
    assert "Interested hain" in r.response_text


async def test_first_objection_cites_the_grounding_chunk():
    # Z: when the reply leans on a KB chunk, it must carry a citation to it.
    r = await PROVIDER.generate(_ctx("mere paas already do cards hain"), PRODUCT)
    assert r.grounded is True
    assert [c.title for c in r.citations] == ["Regalia"]


async def test_smalltalk_is_emitted_and_redirects():
    # AA: smalltalk is in the enum — the stub must actually produce it.
    r = await PROVIDER.generate(_ctx("Namaste, aap kaise ho?"), PRODUCT)
    assert r.intent == Intent.smalltalk
    assert r.end_call is False
    assert "offer" in r.response_text.lower()  # steers back to purpose


async def test_ungrounded_response_without_citation_stays_grounded_when_no_fact():
    # A pure acknowledgement asserts no product fact → grounded, no citation needed.
    r = await PROVIDER.generate(_ctx("nahi chahiye"), PRODUCT)
    assert r.intent == Intent.not_interested
    assert r.citations == []
    assert r.grounded is True
