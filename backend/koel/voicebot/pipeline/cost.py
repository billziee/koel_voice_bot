"""Per-call cost estimation (roadmap O-9).

A deterministic, dependency-free *signal* — not a bill. Each turn's spend is
approximated from the turn's text lengths using blended per-provider rates, so
the orchestrator can accumulate a per-session running cost and enforce a budget
cap without threading real provider-reported units through every hop. Swap in
precise units (audio-seconds, tokens, characters) when true unit economics are
needed; the PRD only asks for a cost signal + a spend ceiling here.
"""

from __future__ import annotations

# Blended $/char rates. NLP mirrors the Groq blended token price used in
# nlp/providers.py (~$0.7 / 1M tokens ≈ 4 chars/token ⇒ ~1.75e-7 $/char). STT and
# TTS are order-of-magnitude proxies over the text each side produced (the
# customer's transcript stands in for their audio; the bot's reply for its TTS).
_USD_PER_STT_CHAR = 1.0e-6
_USD_PER_NLP_CHAR = 1.75e-7
_USD_PER_TTS_CHAR = 1.6e-6


def estimate_turn_cost_usd(*, customer_text: str, bot_text: str) -> float:
    """Estimate one turn's STT + NLP + TTS spend from its text lengths.

    Monotonic in both inputs and always >= 0, so accumulating it yields a
    sensible running per-session cost for the O-9 budget guard.
    """
    cust = len(customer_text or "")
    bot = len(bot_text or "")
    return (
        cust * _USD_PER_STT_CHAR
        + (cust + bot) * _USD_PER_NLP_CHAR
        + bot * _USD_PER_TTS_CHAR
    )
