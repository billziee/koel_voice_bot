"""Gate 2 backchannel classification (PRD §05; §14 false-interrupt rate <5%)."""

from koel.common.schemas import Language
from koel.voicebot.pipeline.bargein import BargeInClassifier


def make() -> BargeInClassifier:
    return BargeInClassifier(backchannel_max_speech_ms=300)


def test_short_utterance_is_not_an_interrupt():
    clf = make()
    decision = clf.evaluate(partial_text="haan", speech_duration_ms=120, language=Language.hinglish)
    assert decision.interrupt is False
    assert decision.reason == "too_short"


def test_backchannel_word_is_ignored_even_when_long_enough():
    clf = make()
    decision = clf.evaluate(partial_text="achha", speech_duration_ms=500, language=Language.hinglish)
    assert decision.interrupt is False
    assert decision.reason == "backchannel"


def test_sustained_real_speech_interrupts():
    clf = make()
    decision = clf.evaluate(
        partial_text="wait I don't want a card",
        speech_duration_ms=900,
        language=Language.en,
    )
    assert decision.interrupt is True
    assert decision.reason == "sustained_speech"


def test_english_backchannels():
    clf = make()
    for word in ("okay", "yeah", "got it"):
        assert clf.is_backchannel(word, Language.en) is True
    assert clf.is_backchannel("cancel everything", Language.en) is False
