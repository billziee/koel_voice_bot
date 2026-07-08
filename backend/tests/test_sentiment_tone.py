"""Sentiment-driven in-call tone adaptation (roadmap 4.3).

Covers the pure trend→tone mapping and the NLP contract that feeds it:
customer_sentiment is parsed from the model, defaults safely, and falls back to
an intent-derived value when the model omits or garbles it.
"""

from __future__ import annotations

import json

from koel.common.schemas import Language, Tone
from koel.nlp.providers import GroqNlpProvider, _sentiment_from_intent
from koel.nlp.schemas import Intent, NlpResponse, Sentiment, TurnContext
from koel.voicebot.pipeline.orchestrator import sentiment_adjusted_tone

NEG = Sentiment.negative.value
POS = Sentiment.positive.value
NEU = Sentiment.neutral.value


# --- pure trend → tone mapping ------------------------------------------------

def test_two_consecutive_negatives_force_empathetic():
    # Even if NLP proposed a cheerful/firm tone, a sustained negative trend softens it.
    assert sentiment_adjusted_tone([NEG, NEG], Tone.cheerful) == Tone.empathetic
    assert sentiment_adjusted_tone([POS, NEG, NEG], Tone.firm) == Tone.empathetic


def test_single_negative_keeps_nlp_tone():
    assert sentiment_adjusted_tone([NEG], Tone.cheerful) == Tone.cheerful
    assert sentiment_adjusted_tone([POS, NEG], Tone.firm) == Tone.firm


def test_recovery_after_negatives_keeps_nlp_tone():
    # A positive turn breaks the negative streak → back to the NLP-chosen tone.
    assert sentiment_adjusted_tone([NEG, NEG, POS], Tone.cheerful) == Tone.cheerful


def test_empty_or_neutral_history_keeps_nlp_tone():
    assert sentiment_adjusted_tone([], Tone.neutral) == Tone.neutral
    assert sentiment_adjusted_tone([NEU, NEU], Tone.cheerful) == Tone.cheerful


# --- NLP contract -------------------------------------------------------------

def test_nlp_response_defaults_sentiment_neutral():
    r = NlpResponse(session_id="s", response_text="hi", intent=Intent.smalltalk, tone=Tone.neutral)
    assert r.customer_sentiment == Sentiment.neutral


def test_intent_sentiment_fallback_mapping():
    assert _sentiment_from_intent(Intent.objection) == Sentiment.negative
    assert _sentiment_from_intent(Intent.opt_out) == Sentiment.negative
    assert _sentiment_from_intent(Intent.high_interest) == Sentiment.positive
    assert _sentiment_from_intent(Intent.smalltalk) == Sentiment.neutral


def _ctx() -> TurnContext:
    return TurnContext(
        session_id="s", bot_id="b", use_case="credit_card",
        language=Language.hinglish, customer_text="nahi chahiye",
    )


def _provider() -> GroqNlpProvider:
    return GroqNlpProvider(api_key="k", model="llama-3.3-70b-versatile")


def test_parse_uses_explicit_customer_sentiment():
    raw = json.dumps({
        "response_text": "samajh sakti hoon.", "intent": "objection",
        "tone": "empathetic", "customer_sentiment": "negative",
    })
    r = _provider()._parse(_ctx(), [], raw)
    assert r.customer_sentiment == Sentiment.negative


def test_parse_falls_back_to_intent_when_sentiment_missing_or_bad():
    # Missing → derived from the (negative) intent.
    raw = json.dumps({"response_text": "ok", "intent": "not_interested", "tone": "empathetic"})
    assert _provider()._parse(_ctx(), [], raw).customer_sentiment == Sentiment.negative
    # Garbled value → same fallback.
    raw2 = json.dumps({
        "response_text": "ok", "intent": "high_interest",
        "tone": "cheerful", "customer_sentiment": "???",
    })
    assert _provider()._parse(_ctx(), [], raw2).customer_sentiment == Sentiment.positive
