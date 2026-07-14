"""Additional Indian languages via Sarvam (4.1)."""

from __future__ import annotations

from koel.common.schemas import SUPPORTED_LANGUAGES, Language
from koel.stt.providers import _WHISPER_FORCE
from koel.tts.config import TtsSettings
from koel.tts.providers import RoutingTtsProvider, SarvamTtsProvider, StubOrpheusProvider


def test_new_languages_are_first_class_enum_members():
    assert {Language.ta, Language.te, Language.mr, Language.bn} <= set(Language)
    assert {Language.ta, Language.te, Language.mr, Language.bn} <= SUPPORTED_LANGUAGES


def test_sarvam_maps_each_new_language_to_its_code():
    codes = SarvamTtsProvider._LANG_CODE
    assert codes["ta"] == "ta-IN"
    assert codes["te"] == "te-IN"
    assert codes["mr"] == "mr-IN"
    assert codes["bn"] == "bn-IN"


def test_stt_pins_each_new_language_for_whisper():
    for lang in (Language.ta, Language.te, Language.mr, Language.bn):
        assert _WHISPER_FORCE[lang] == lang.value


def test_sarvam_language_set_includes_the_new_languages():
    langs = TtsSettings().sarvam_language_set
    assert {"ta", "te", "mr", "bn", "hi", "hinglish"} <= langs


def test_routing_sends_a_new_language_to_the_sarvam_provider():
    default = StubOrpheusProvider()
    sarvam = StubOrpheusProvider()  # stand-in for a Sarvam provider
    router = RoutingTtsProvider(
        default=default, hindi=sarvam, hindi_languages={"hi", "hinglish", "ta", "te", "mr", "bn"}
    )

    from koel.common.schemas import Tone
    from koel.tts.schemas import SynthesizeRequest

    def _primary(lang: Language):
        req = SynthesizeRequest(session_id="s", text="…", language=lang, tone=Tone.neutral)
        return router._pick(req)[0]

    assert _primary(Language.ta) is sarvam  # Tamil → Sarvam
    assert _primary(Language.en) is default  # English → base, unchanged
