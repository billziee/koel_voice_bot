"""Silence / no-input handling (PRD §05 resilience table).

Rules enforced by VB_Svc:
  - No speech for 5s after a bot prompt  → re-prompt once.
  - No speech for a further 5s           → polite close, outcome
    ``customer_unresponsive``, CRM flagged for retry.
  - Non-speech audio (noise) does NOT reset the no-input timer — the client-side
    VAD energy threshold filters it, so noise never reaches this logic as speech.

Pure decision logic, driven by the session's ``no_input_strikes`` counter.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from koel.common.schemas import Language
from koel.voicebot.pipeline import localization

# hi is voiced by Magpie/Sarvam → MUST be Devanagari (romanized comes out as
# gibberish), hand-authored. hinglish is voiced by Orpheus → romanized Latin,
# derived from hi via Sarvam transliteration at startup (see localization.py).
REPROMPT_TEXT = localization.register(localization.Localized(
    en="Hello, are you still there?",
    hi="क्या आप मुझे सुन पा रहे हैं?",
    hinglish_fallback="Hello, aap sun pa rahe hain? Are you still there?",
))

CLOSE_TEXT = localization.register(localization.Localized(
    en="It seems you're busy. I'll call back later. Thank you.",
    hi="लगता है आप व्यस्त हैं। मैं बाद में कॉल करूँगी। धन्यवाद।",
    hinglish_fallback="Lagta hai aap busy hain. Main baad mein call karti hoon. Dhanyavaad.",
))


class NoInputAction(str, enum.Enum):
    reprompt = "reprompt"
    close = "close"


@dataclass(frozen=True)
class NoInputOutcome:
    action: NoInputAction
    text: str
    event: str  # logged on the session: NO_INPUT_1 / customer_unresponsive


def on_no_input(strikes: int, language: Language) -> NoInputOutcome:
    """Map the current strike count to the next behaviour.

    strikes == 0 → first timeout → re-prompt.
    strikes >= 1 → second timeout → close as unresponsive.
    """
    lang = language if language in REPROMPT_TEXT else Language.hinglish
    if strikes == 0:
        return NoInputOutcome(
            action=NoInputAction.reprompt,
            text=REPROMPT_TEXT[lang],
            event="NO_INPUT_1",
        )
    return NoInputOutcome(
        action=NoInputAction.close,
        text=CLOSE_TEXT[lang],
        event="customer_unresponsive",
    )
