"""Telephony barge-in Gate 1+2 wiring (PRD §05) — partial-transcript barge-in
for the carrier transport, the half that was missing.

The browser transport gets Gate 1 (voice detected) for free from its
client-side VAD worklet, which reports its own measured speech duration
alongside a barge_in_candidate. Twilio has no such client: the interim STT
partial's mere non-empty arrival while the bot is speaking stands in for Gate
1 here, and this class layers Gate 2 (:class:`BargeInClassifier`) on top,
approximating ``speech_duration_ms`` as wall-clock elapsed since the
utterance's first partial arrived (Deepgram interims carry no authoritative
duration of their own).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from koel.common.schemas import Language
from koel.voicebot.pipeline.bargein import BargeInClassifier, BargeInDecision


class TelephonyBargeInGate:
    """One instance per call. Callers must check ``is_bot_speaking`` (the
    bridge's own flag) themselves — this class has no way to know on its own
    and only tracks the elapsed-time state for whichever utterance is
    currently in flight."""

    def __init__(
        self, classifier: BargeInClassifier, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._classifier = classifier
        self._clock = clock
        self._started_at: float | None = None

    def reset(self) -> None:
        """Call when the bot stops speaking or a final transcript lands —
        clears the in-flight utterance's elapsed-time tracking so the NEXT
        utterance starts its own duration count from zero."""
        self._started_at = None

    def evaluate_partial(
        self,
        *,
        text: str,
        language: Language,
        extra_backchannels: set[str] | None = None,
    ) -> BargeInDecision:
        """Gate 2 for one interim partial. Resets the elapsed-time tracker on
        a confirmed interrupt (the next utterance, if any, starts fresh)."""
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
        duration_ms = int((now - self._started_at) * 1000)
        decision = self._classifier.evaluate(
            partial_text=text,
            speech_duration_ms=duration_ms,
            language=language,
            extra_backchannels=extra_backchannels,
        )
        if decision.interrupt:
            self._started_at = None
        return decision
