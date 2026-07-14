"""Verbal-consent detection over the transcript (2.3, PRD §04/§11).

Deterministic affirmation matching (EN / HI / Hinglish) so the consent evidence
is reproducible, not an LLM judgement. ``find_consent_turn`` locates the first
customer turn that affirms *after* the bot has spoken (the disclosure), which is
the turn a ConsentRecord anchors to.
"""

from __future__ import annotations

from collections.abc import Sequence

from koel.voicebot.session.models import Turn

# Affirmations across EN / HI (Devanagari + romanized) / Hinglish. Matched as
# whole-word / phrase substrings on the normalised text.
_AFFIRMATIVE = (
    "yes", "yeah", "yep", "i agree", "agreed", "agree", "i consent", "consent",
    "confirm", "confirmed", "okay", "ok", "sure", "go ahead", "please do",
    "haan", "haanji", "haan ji", "ji haan", "theek hai", "thik hai", "bilkul",
    "sahmat", "kar do", "kar dijiye", "manzoor", "हाँ", "जी हाँ", "ठीक है", "सहमत",
)
# Negations that must veto a match (e.g. "no, I don't agree", "nahi").
_NEGATIVE = ("no ", "not ", "n't", "nahi", "mat ", "नहीं", "नही")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def is_affirmative(text: str) -> bool:
    """True when the utterance is an explicit consent affirmation and not negated."""
    norm = f" {_norm(text)} "
    if any(neg in norm for neg in _NEGATIVE):
        return False
    return any(f" {a} " in norm or norm.strip() == a for a in _AFFIRMATIVE)


def find_consent_turn(transcript: Sequence[Turn]) -> Turn | None:
    """The first customer turn that affirms after the bot has spoken a disclosure,
    or None. Requires a preceding bot turn so a stray 'haan' before any disclosure
    doesn't count as consent."""
    bot_has_spoken = False
    for turn in transcript:
        if turn.speaker == "bot":
            bot_has_spoken = True
        elif turn.speaker == "customer" and bot_has_spoken and is_affirmative(turn.text or ""):
            return turn
    return None
