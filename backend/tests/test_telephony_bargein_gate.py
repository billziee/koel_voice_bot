"""Telephony partial-transcript barge-in (Gate 1+2 wiring for the carrier
transport — the browser transport's client VAD worklet has no telephony
equivalent, so the interim STT partial itself stands in for Gate 1)."""

from __future__ import annotations

from koel.common.schemas import Language
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.telephony.bargein_gate import TelephonyBargeInGate


def _classifier() -> BargeInClassifier:
    return BargeInClassifier(backchannel_max_speech_ms=300)


class _FakeClock:
    """Deterministic clock — advance() steps it forward by a fixed amount."""

    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_first_partial_has_zero_duration_and_is_too_short():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    decision = gate.evaluate_partial(text="wait", language=Language.en)
    assert decision.interrupt is False
    assert decision.reason == "too_short"  # 0ms elapsed on the very first partial


def test_duration_accumulates_across_partials_of_the_same_utterance():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    gate.evaluate_partial(text="wait", language=Language.en)  # starts the clock
    clock.advance(0.5)  # 500ms later, still speaking
    decision = gate.evaluate_partial(text="wait I don't want this", language=Language.en)
    assert decision.interrupt is True
    assert decision.reason == "sustained_speech"


def test_backchannel_never_interrupts_regardless_of_duration():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    gate.evaluate_partial(text="haan", language=Language.hinglish)
    clock.advance(1.0)
    decision = gate.evaluate_partial(text="haan", language=Language.hinglish)
    assert decision.interrupt is False
    assert decision.reason == "backchannel"


def test_reset_restarts_the_duration_clock_for_the_next_utterance():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    gate.evaluate_partial(text="wait", language=Language.en)
    clock.advance(1.0)
    gate.reset()  # bot stopped speaking / final landed
    # A brand new utterance starts from zero elapsed again.
    decision = gate.evaluate_partial(text="hello again", language=Language.en)
    assert decision.interrupt is False
    assert decision.reason == "too_short"


def test_confirmed_interrupt_resets_the_clock_for_the_next_utterance():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    gate.evaluate_partial(text="wait", language=Language.en)
    clock.advance(1.0)
    interrupted = gate.evaluate_partial(text="wait I don't want this", language=Language.en)
    assert interrupted.interrupt is True
    # Without an explicit reset, the NEXT utterance's first partial should
    # still start its own duration count from zero, not carry over 1s.
    decision = gate.evaluate_partial(text="something new", language=Language.en)
    assert decision.interrupt is False
    assert decision.reason == "too_short"


def test_extra_backchannels_are_honoured():
    clock = _FakeClock()
    gate = TelephonyBargeInGate(_classifier(), clock=clock)
    gate.evaluate_partial(text="continue please", language=Language.en)  # primes the clock
    clock.advance(1.0)
    decision = gate.evaluate_partial(
        text="continue please", language=Language.en, extra_backchannels={"continue please"}
    )
    assert decision.interrupt is False
    assert decision.reason == "backchannel"
