"""No-input escalation rules + filler rotation (PRD §05)."""

from koel.common.schemas import Language
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.silence import NoInputAction, on_no_input


def test_first_timeout_reprompts():
    outcome = on_no_input(strikes=0, language=Language.hinglish)
    assert outcome.action == NoInputAction.reprompt
    assert outcome.event == "NO_INPUT_1"


def test_second_timeout_closes_as_unresponsive():
    outcome = on_no_input(strikes=1, language=Language.hinglish)
    assert outcome.action == NoInputAction.close
    assert outcome.event == "customer_unresponsive"


def test_filler_rotates_and_never_repeats_consecutively():
    cache = FillerCache()
    cache.load(Language.hinglish, ["a", "b", "c"])
    seen = [cache.next_clip("sess1", Language.hinglish)[1] for _ in range(3)]
    assert seen == ["a", "b", "c"]  # rotates in order
    # next wraps around
    assert cache.next_clip("sess1", Language.hinglish)[1] == "a"


def test_filler_not_ready_returns_none():
    cache = FillerCache()
    assert cache.next_clip("sess1", Language.en) is None
    assert cache.is_ready(Language.en) is False
