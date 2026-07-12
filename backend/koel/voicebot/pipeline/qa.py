"""Deterministic post-call script-adherence QA (5.5, PRD §06 compliance audit).

Rule-based, not LLM-judged: "did the bot make the mandated disclosures, avoid
prohibited phrases, and cover the required flow intents?" are all checkable by
matching the transcript — so the score is deterministic, explainable, and needs
no model call (the answer a compliance auditor can reproduce byte-for-byte).

Rules live per bot in ``BotConfig.config['qa_rules']`` and are seeded onto the
session; empty/absent rules ⇒ QA is skipped (returns None). Runs off the
critical path — computed once when the finished call is persisted.
"""

from __future__ import annotations

from collections.abc import Sequence

from koel.voicebot.session.models import Turn


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace for tolerant substring matching."""
    return " ".join(text.lower().split())


def score_transcript(turns: Sequence[Turn], rules: dict | None) -> dict | None:
    """Score one call's transcript against a bot's QA rules. Returns None when
    there are no rules to check.

    Rules (all optional lists of strings):
      - ``required_disclosures``: must each appear in some bot turn.
      - ``prohibited_phrases``: must NOT appear in any bot turn.
      - ``required_intents``: must each appear among the turns' intents.
    """
    if not rules:
        return None

    bot_text = _norm(" ".join(t.text for t in turns if t.speaker == "bot" and t.text))
    intents = {t.intent for t in turns if t.intent}

    required_disclosures = [str(d) for d in rules.get("required_disclosures", []) if str(d).strip()]
    prohibited_phrases = [str(p) for p in rules.get("prohibited_phrases", []) if str(p).strip()]
    required_intents = [str(i) for i in rules.get("required_intents", []) if str(i).strip()]

    missing_disclosures = [d for d in required_disclosures if _norm(d) not in bot_text]
    prohibited_hits = [p for p in prohibited_phrases if _norm(p) in bot_text]
    missing_intents = [i for i in required_intents if i not in intents]

    checks = len(required_disclosures) + len(prohibited_phrases) + len(required_intents)
    violations = len(missing_disclosures) + len(prohibited_hits) + len(missing_intents)
    score = round((checks - violations) / checks, 4) if checks else 1.0
    # A call "passes" only on the hard controls: every disclosure made, nothing
    # prohibited said. Missing flow intents lower the score but aren't a hard fail.
    passed = not missing_disclosures and not prohibited_hits

    return {
        "score": score,
        "passed": passed,
        "disclosures_met": [d for d in required_disclosures if _norm(d) in bot_text],
        "missing_disclosures": missing_disclosures,
        "prohibited_hits": prohibited_hits,
        "intents_met": [i for i in required_intents if i in intents],
        "missing_intents": missing_intents,
        "checks": checks,
        "violations": violations,
    }
