"""TTS_svc FastAPI app. Run: uvicorn koel.tts.main:app --port 8103"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.tts.config import get_tts_settings
from koel.tts.providers import (
    GroqOrpheusProvider,
    NvidiaMagpieTtsProvider,
    RoutingTtsProvider,
    SarvamTtsProvider,
    StubOrpheusProvider,
    TtsProvider,
)
from koel.tts.schemas import SynthesisResult, SynthesizeRequest


def _build_provider() -> TtsProvider:
    settings = get_tts_settings()
    # Base (English / default) provider.
    base: TtsProvider = (
        GroqOrpheusProvider(
            api_key=settings.groq_api_key,  # type: ignore[arg-type]  # groq_enabled guards None
            model=settings.groq_orpheus_model,
            voice=settings.groq_voice,
            base_url=settings.groq_base_url,
            audio_format=settings.groq_audio_format,
            timeout_s=settings.groq_timeout_s,
        )
        if settings.groq_enabled
        else StubOrpheusProvider()
    )
    # Magpie is Devanagari-only (romanized text comes out as gibberish), so it's
    # built standalone here and only ever wired in as SarvamTtsProvider's
    # hi-specific fallback below — never as a direct Hinglish handler.
    magpie: TtsProvider | None = None
    if settings.nvidia_enabled:
        magpie = NvidiaMagpieTtsProvider(
            api_key=settings.nvidia_api_key,  # type: ignore[arg-type]  # nvidia_enabled guards None
            function_id=settings.nvidia_tts_function_id,
            uri=settings.nvidia_tts_uri,
            voice=settings.nvidia_tts_voice,
            sample_rate=settings.nvidia_tts_sample_rate,
            timeout_s=settings.nvidia_tts_timeout_s,
            fallback=base,
        )
    # hi + hinglish cascade: Sarvam (primary, if enabled) handles both — verified
    # live that Bulbul voices romanized Hinglish correctly, unlike Magpie. Sarvam
    # falls back to Magpie only for hi (same Devanagari script family) and
    # straight to base for hinglish/en (Magpie would mangle romanized text).
    hindi: TtsProvider = magpie if magpie is not None else base
    if settings.sarvam_enabled:
        hindi = SarvamTtsProvider(
            api_key=settings.sarvam_api_key,  # type: ignore[arg-type]  # sarvam_enabled guards None
            model=settings.sarvam_tts_model,
            speaker=settings.sarvam_speaker,
            base_url=settings.sarvam_base_url,
            audio_format=settings.sarvam_audio_format,
            sample_rate=settings.sarvam_sample_rate,
            timeout_s=settings.sarvam_timeout_s,
            fallback=base,
            hi_fallback=magpie,
        )
    if hindi is base:
        return base
    # Without Sarvam, Magpie can only take hi (never hinglish) — restrict the
    # routed set accordingly regardless of what's configured.
    hindi_languages = settings.nvidia_language_set
    if not settings.sarvam_enabled:
        hindi_languages = hindi_languages & {"hi"}
    return RoutingTtsProvider(default=base, hindi=hindi, hindi_languages=hindi_languages)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    aclose = getattr(_provider, "aclose", None)
    if aclose is not None:
        await aclose()


app = create_app(
    service_name="tts_svc",
    title="KOEL · TTS_svc",
    description="Tone-aware neural TTS — Groq Orpheus or deterministic stub.",
    lifespan=_lifespan,
)

_provider = _build_provider()
router = APIRouter(prefix="/v1", tags=["tts"])


@router.post("/synthesize", response_model=SynthesisResult)
async def synthesize(request: SynthesizeRequest, _: ServicePrincipal) -> SynthesisResult:
    """Synthesize a complete clip (used for greetings and the filler cache)."""
    with span("tts.synthesize", session_id=request.session_id, tone=request.tone.value):
        return await _provider.synthesize(request)


@router.post("/stream")
async def synthesize_stream(request: SynthesizeRequest, _: ServicePrincipal) -> StreamingResponse:
    """Stream NDJSON audio chunks so VB_Svc can issue TTS_KILL mid-utterance."""
    request.stream = True

    async def _gen():
        with span("tts.stream", session_id=request.session_id, tone=request.tone.value):
            async for chunk in _provider.stream(request):
                yield chunk.model_dump_json() + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


app.include_router(router)
