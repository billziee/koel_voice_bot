"""Per-bot persona voice override (4.2)."""

from __future__ import annotations

from koel.common.schemas import Language, Tone
from koel.tts.providers import GroqOrpheusProvider, StubOrpheusProvider
from koel.tts.schemas import SynthesizeRequest


def _req(**over) -> SynthesizeRequest:
    base = dict(session_id="s", text="namaste", language=Language.hinglish, tone=Tone.neutral)
    return SynthesizeRequest(**{**base, **over})


def test_voice_defaults_to_none_for_backward_compatibility():
    assert _req().voice is None


def test_orpheus_explicit_voice_overrides_gender_default():
    p = GroqOrpheusProvider(api_key="k", model="m", voice="autumn")
    # No explicit voice → gender-derived default.
    assert p._voice_for(_req(voice_gender="male")) == "austin"
    assert p._voice_for(_req(voice_gender="female")) == "autumn"
    # Explicit per-bot voice wins over gender.
    assert p._voice_for(_req(voice="diana", voice_gender="male")) == "diana"


async def test_stub_still_synthesizes_with_a_voice_set():
    # The override is inert for the stub, but must not break the contract.
    result = await StubOrpheusProvider().synthesize(_req(voice="priya"))
    assert result.audio_b64
