"""Dual-Gated Interruption Engine (PRD §05).

Gate 1 (Voice Activity Detection) runs client-side and only flags that audio
*started* — it makes no decision. It arrives here as a BARGE_IN_CANDIDATE event.

Gate 2 (Backchannel Classification) is the smart gate evaluated here on the
partial transcript: an utterance is a backchannel (→ ignore, keep talking) if it
is shorter than the configured speech threshold OR matches the per-language
backchannel dictionary. Otherwise it is a real interrupt → TTS_KILL.

This module is pure decision logic so it is trivially unit-testable against the
PRD §14 "Backchannel False-Interrupt Rate <5%" target.
"""

from __future__ import annotations

from dataclasses import dataclass

from koel.common.schemas import Language

# Per-language backchannel dictionary (PRD §05). Configurable per bot instance;
# these are the defaults. Matching is on the normalised partial transcript.
DEFAULT_BACKCHANNELS: dict[Language, set[str]] = {
    Language.en: {"hmm", "mhm", "yeah", "yes", "okay", "ok", "right", "uh huh", "got it", "sure"},
    Language.hi: {"haan", "ji", "achha", "theek", "theek hai", "hmm", "accha", "ok"},
    Language.hinglish: {
        "haan", "achha", "accha", "okay", "ok", "theek", "theek hai", "hmm",
        "ji", "yeah", "right", "haan ji",
    },
}


@dataclass(frozen=True)
class BargeInDecision:
    interrupt: bool  # True → issue TTS_KILL
    reason: str  # "backchannel" | "too_short" | "sustained_speech"


class BargeInClassifier:
    def __init__(
        self,
        *,
        backchannel_max_speech_ms: int,
        dictionaries: dict[Language, set[str]] | None = None,
    ) -> None:
        self._max_ms = backchannel_max_speech_ms
        self._dicts = dictionaries or DEFAULT_BACKCHANNELS

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join(text.lower().strip().split())

    def is_backchannel(
        self, text: str, language: Language, *, extra: set[str] | None = None
    ) -> bool:
        norm = self._normalise(text)
        if not norm:
            return True
        vocab = self._dicts.get(language, set())
        if extra:
            vocab = vocab | extra
        return norm in vocab

    def evaluate(
        self,
        *,
        partial_text: str,
        speech_duration_ms: int,
        language: Language,
        extra_backchannels: set[str] | None = None,
    ) -> BargeInDecision:
        """Gate 2. Called only after Gate 1 fired a BARGE_IN_CANDIDATE.

        ``extra_backchannels`` lets the caller fold in a per-bot dictionary
        (PRD §05: "configurable per bot instance and language") on top of the
        per-language defaults without mutating the shared classifier.
        """
        # Sub-threshold speech is passive listening, not an interrupt.
        if speech_duration_ms < self._max_ms:
            return BargeInDecision(interrupt=False, reason="too_short")
        if self.is_backchannel(partial_text, language, extra=extra_backchannels):
            return BargeInDecision(interrupt=False, reason="backchannel")
        return BargeInDecision(interrupt=True, reason="sustained_speech")
