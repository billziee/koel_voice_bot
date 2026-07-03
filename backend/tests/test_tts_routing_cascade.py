"""RoutingTtsProvider fallback cascade + persona voice mapping (E, G)."""

from __future__ import annotations

import base64

import pytest

from koel.common.schemas import Language, Tone
from koel.tts.providers import (
    GroqOrpheusProvider,
    RoutingTtsProvider,
    TtsProvider,
    resolve_prosody,
)
from koel.tts.schemas import SynthesisResult, SynthesizeRequest


class _GoodProvider(TtsProvider):
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(self.tag.encode()).decode(),
            duration_ms=100,
            prosody=resolve_prosody(request),
        )

    async def stream(self, request):  # pragma: no cover - unused
        yield  # type: ignore[misc]


class _FailProvider(TtsProvider):
    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        raise RuntimeError("provider down")

    async def stream(self, request):  # pragma: no cover - unused
        raise RuntimeError("provider down")
        yield  # unreachable


def _req(lang: Language) -> SynthesizeRequest:
    return SynthesizeRequest(session_id="s", text="hi", language=lang, tone=Tone.neutral)


_HINDI = {"hi", "hinglish"}


async def test_hindi_routes_to_hindi_provider():
    r = RoutingTtsProvider(default=_GoodProvider("EN"), hindi=_GoodProvider("HI"), hindi_languages=_HINDI)
    out = await r.synthesize(_req(Language.hinglish))
    assert base64.b64decode(out.audio_b64) == b"HI"
    assert out.fallback_used is False


async def test_cascades_to_fallback_and_flags_it():
    # Hindi primary fails → cascade to the English default, mark fallback_used.
    r = RoutingTtsProvider(default=_GoodProvider("EN"), hindi=_FailProvider(), hindi_languages=_HINDI)
    out = await r.synthesize(_req(Language.hinglish))
    assert base64.b64decode(out.audio_b64) == b"EN"
    assert out.fallback_used is True


async def test_both_down_raises():
    r = RoutingTtsProvider(default=_FailProvider(), hindi=_FailProvider(), hindi_languages={"hi"})
    with pytest.raises(RuntimeError):
        await r.synthesize(_req(Language.en))


def test_orpheus_voice_by_gender():
    p = GroqOrpheusProvider(api_key="k", model="m", voice="diana")
    male = SynthesizeRequest(session_id="s", text="hi", language=Language.en, voice_gender="male")
    assert p._voice_for(male) == "austin"
    female = SynthesizeRequest(session_id="s", text="hi", language=Language.en, voice_gender="female")
    assert p._voice_for(female) == "autumn"
    # Unknown/blank gender → the configured default voice.
    other = SynthesizeRequest(session_id="s", text="hi", language=Language.en, voice_gender="")
    assert p._voice_for(other) == "diana"


def test_magpie_sample_rate_matches_canonical():
    # Magpie MUST emit at the pipeline's canonical rate. It was 44100 while the
    # mixer/concat/browser assumed 24000 → Hindi/Devanagari played ~1.84x fast
    # (high-pitched gibberish). Guards against that drift regressing.
    from koel.tts.config import TtsSettings
    from koel.tts.schemas import CANONICAL_SAMPLE_RATE

    assert TtsSettings().nvidia_tts_sample_rate == CANONICAL_SAMPLE_RATE
