"""Latency-masking filler audio (PRD §05).

If NLP_svc has not produced a first token within the filler trigger window
(default 300ms) of the customer's end-of-utterance, VB_Svc injects a
pre-rendered, language- and tone-matched filler clip with sub-100ms first byte.
Fillers rotate to avoid repetition and are never injected twice consecutively
within the same turn.

In the boilerplate the clips are synthesised once at startup via TTS_svc and
cached in memory; in production they are pre-rendered assets on a local disk.
"""

from __future__ import annotations

from collections import defaultdict

from koel.common.schemas import Language, Tone

# Filler text per language. Tone selects energy when these are synthesised.
FILLER_PHRASES: dict[Language, list[str]] = {
    Language.en: ["Hmm…", "Let me pull that up…", "One moment please…"],
    # hi → Magpie, so Devanagari only. hinglish → Orpheus, so romanized Latin.
    Language.hi: ["हम्म…", "एक सेकंड कीजिए…", "मैं देख रही हूँ…"],
    Language.hinglish: ["Hmm…", "Ek second kijiye…", "Let me check…", "Bas ek minute…"],
}


class FillerCache:
    """Holds pre-rendered filler audio keyed by (language, index) and rotates them."""

    def __init__(self) -> None:
        # (language) -> list[audio_b64]
        self._audio: dict[Language, list[str]] = {}
        # last index used per session+language, to avoid back-to-back repeats.
        self._cursor: dict[tuple[str, Language], int] = defaultdict(lambda: -1)

    def load(self, language: Language, clips_b64: list[str]) -> None:
        self._audio[language] = clips_b64

    def phrases(self, language: Language) -> list[str]:
        return FILLER_PHRASES.get(language, FILLER_PHRASES[Language.en])

    def is_ready(self, language: Language) -> bool:
        return bool(self._audio.get(language))

    def next_clip(self, session_id: str, language: Language) -> tuple[str, str] | None:
        """Return (text, audio_b64) for the next filler, rotated; None if not loaded."""
        clips = self._audio.get(language)
        phrases = self.phrases(language)
        if not clips:
            return None
        key = (session_id, language)
        idx = (self._cursor[key] + 1) % len(clips)
        self._cursor[key] = idx
        return phrases[idx % len(phrases)], clips[idx]


def filler_tone_for(use_case: str) -> Tone:
    """Filler should match the call's emotional register (PRD §05/§06)."""
    if use_case == "collections":
        return Tone.empathetic
    if use_case in ("credit_card", "personal_loan"):
        return Tone.cheerful
    return Tone.neutral
