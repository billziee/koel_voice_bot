"""Language-routed NLP model selection (latency: en→8b, hi/hinglish→70b)."""

from __future__ import annotations

from koel.common.schemas import Language
from koel.nlp.providers import GroqNlpProvider
from koel.nlp.schemas import TurnContext


def _ctx(lang: Language) -> TurnContext:
    return TurnContext(session_id="s", bot_id="b", use_case="credit_card",
                       language=lang, customer_text="hi")


def test_english_uses_fast_model_others_use_quality():
    p = GroqNlpProvider(
        api_key="k", model="quality-default",
        model_fast="llama-3.1-8b-instant", model_quality="llama-3.3-70b-versatile",
    )
    assert p._model_for(_ctx(Language.en)) == "llama-3.1-8b-instant"
    assert p._model_for(_ctx(Language.hi)) == "llama-3.3-70b-versatile"
    assert p._model_for(_ctx(Language.hinglish)) == "llama-3.3-70b-versatile"


def test_routing_falls_back_to_single_model_when_unsplit():
    p = GroqNlpProvider(api_key="k", model="only-model")
    assert p._model_for(_ctx(Language.en)) == "only-model"
    assert p._model_for(_ctx(Language.hinglish)) == "only-model"
