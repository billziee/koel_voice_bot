"""TTS_svc FastAPI app. Run: uvicorn koel.tts.main:app --port 8103"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.logging import get_logger
from koel.common.telemetry import span
from koel.tts.config import get_tts_settings
from koel.tts.providers import (
    DeepgramTtsProvider,
    GroqOrpheusProvider,
    NvidiaMagpieTtsProvider,
    OpenAiCompatibleTtsProvider,
    RoutingTtsProvider,
    SarvamTtsProvider,
    StubOrpheusProvider,
    TtsProvider,
)
from koel.tts.schemas import CANONICAL_SAMPLE_RATE, SynthesisResult, SynthesizeRequest

logger = get_logger("koel.tts_svc")


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
            ws_enabled=settings.sarvam_tts_ws_enabled,
        )
    if hindi is base:
        return base
    # With Sarvam, it handles its full language set (hi/hinglish + the 4.1 Indian
    # languages). Without it, Magpie can only take hi (never hinglish or the other
    # scripts) — restrict the routed set accordingly regardless of config.
    if settings.sarvam_enabled:
        hindi_languages = settings.sarvam_language_set
    else:
        hindi_languages = settings.nvidia_language_set & {"hi"}
    return RoutingTtsProvider(default=base, hindi=hindi, hindi_languages=hindi_languages)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # Release the default provider + any BYOK per-connection HTTP client pools.
    byok = [p for _, p in _conn_providers.values()]
    for obj in (_provider, *byok):
        aclose = getattr(obj, "aclose", None)
        if aclose is not None:
            await aclose()


app = create_app(
    service_name="tts_svc",
    title="KOEL · TTS_svc",
    description="Tone-aware neural TTS — Groq Orpheus or deterministic stub.",
    lifespan=_lifespan,
)

_settings = get_tts_settings()
_provider = _build_provider()
router = APIRouter(prefix="/v1", tags=["tts"])

# BYOK per-connection TTS providers (Phase 3), cached by a signature of the
# resolved connection so an edit (new key/url/model/params) rebuilds instead of
# serving a stale client. Bounded by the number of distinct custom connections.
_conn_providers: dict[str, tuple[str, TtsProvider]] = {}


def _conn_signature(resolved) -> str:  # noqa: ANN001 — ResolvedConnection
    p = resolved.params or {}
    return (
        f"{resolved.provider}|{resolved.base_url}|{resolved.model}|{hash(resolved.api_key)}"
        f"|{p.get('voice')}|{p.get('sample_rate')}"
    )


def _build_conn_provider(resolved) -> TtsProvider | None:  # noqa: ANN001 — ResolvedConnection
    """Build a BYOK TTS provider from a resolved connection, keyed by its
    ``provider`` label. Returns None for an unknown label so the caller degrades
    to the service default. Each provider wraps the default as its own fallback,
    so a live call is never dropped even when the custom endpoint errors."""
    params = resolved.params or {}
    voice = params.get("voice")
    sample_rate = int(params.get("sample_rate", CANONICAL_SAMPLE_RATE))
    label = (resolved.provider or "").lower()
    if label == "deepgram":
        return DeepgramTtsProvider(
            api_key=resolved.api_key,
            model=resolved.model or "aura-2-thalia-en",
            base_url=resolved.base_url or "https://api.deepgram.com",
            voice=voice,
            sample_rate=sample_rate,
            timeout_s=_settings.groq_timeout_s,
            fallback=_provider,
        )
    if label == "openai_compatible":
        return OpenAiCompatibleTtsProvider(
            api_key=resolved.api_key,
            model=resolved.model,
            base_url=resolved.base_url,
            voice=voice,
            audio_format=_settings.groq_audio_format,
            timeout_s=_settings.groq_timeout_s,
            fallback=_provider,
        )
    if label == "sarvam":
        return SarvamTtsProvider(
            api_key=resolved.api_key,
            model=resolved.model or _settings.sarvam_tts_model,
            speaker=voice or _settings.sarvam_speaker,
            base_url=resolved.base_url or _settings.sarvam_base_url,
            audio_format=_settings.sarvam_audio_format,
            sample_rate=sample_rate,
            timeout_s=_settings.sarvam_timeout_s,
            fallback=_provider,
        )
    return None


async def _provider_for(request: SynthesizeRequest) -> TtsProvider:
    """The TTS provider for this clip: a per-bot BYOK connection when set, else the
    service default. BYOK never breaks a clip — any resolve/build failure (no key,
    inactive, wrong kind, unknown provider, undecryptable, secrets disabled) falls
    back to the default."""
    if not request.tts_connection_id:
        return _provider
    try:
        from koel.common.model_connections import resolve_connection

        resolved = await resolve_connection(request.tts_connection_id)
        if resolved is None or resolved.kind != "tts":
            return _provider
        sig = _conn_signature(resolved)
        cached = _conn_providers.get(request.tts_connection_id)
        if cached is not None and cached[0] == sig:
            return cached[1]
        provider = _build_conn_provider(resolved)
        if provider is None:  # unknown provider label → degrade to default
            return _provider
        if cached is not None:  # rebuild on edit → release the stale client
            old = cached[1]
            aclose = getattr(old, "aclose", None)
            if aclose is not None:
                await aclose()
        _conn_providers[request.tts_connection_id] = (sig, provider)
        return provider
    except Exception:  # noqa: BLE001 — BYOK is best-effort; never break the clip
        logger.warning("byok_tts_provider_failed conn=%s; using default", request.tts_connection_id)
        return _provider


@router.post("/synthesize", response_model=SynthesisResult)
async def synthesize(request: SynthesizeRequest, _: ServicePrincipal) -> SynthesisResult:
    """Synthesize a complete clip (used for greetings and the filler cache)."""
    provider = await _provider_for(request)  # BYOK per-bot TTS connection or the default
    with span("tts.synthesize", session_id=request.session_id, tone=request.tone.value):
        return await provider.synthesize(request)


@router.post("/stream")
async def synthesize_stream(request: SynthesizeRequest, _: ServicePrincipal) -> StreamingResponse:
    """Stream NDJSON audio chunks so VB_Svc can issue TTS_KILL mid-utterance."""
    request.stream = True
    provider = await _provider_for(request)  # BYOK per-bot TTS connection or the default

    async def _gen():
        with span("tts.stream", session_id=request.session_id, tone=request.tone.value):
            async for chunk in provider.stream(request):
                yield chunk.model_dump_json() + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.post("/warmup")
async def warmup(_: ServicePrincipal) -> dict[str, bool]:
    """Prime provider connection pools ahead of a session's first turn (O-1).
    VB_Svc fires this at WS accept; best-effort (warm() never raises)."""
    await _provider.warm()
    return {"warmed": True}


app.include_router(router)
