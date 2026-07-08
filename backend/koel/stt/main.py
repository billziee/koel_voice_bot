"""STT_svc FastAPI app. Run: uvicorn koel.stt.main:app --port 8101"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.stt.config import get_stt_settings
from koel.stt.providers import GroqWhisperProvider, SarvamSttProvider, SttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult


def _build_provider() -> SttProvider:
    settings = get_stt_settings()
    provider: SttProvider = StubWhisperProvider()
    if settings.groq_enabled:
        provider = GroqWhisperProvider(
            api_key=settings.groq_api_key,  # type: ignore[arg-type]  # groq_enabled guards None
            model=settings.groq_whisper_model,
            base_url=settings.groq_base_url,
            timeout_s=settings.groq_timeout_s,
        )
    # Sarvam Saaras is primary when enabled; Whisper (or the stub) is its fallback.
    if settings.sarvam_enabled:
        provider = SarvamSttProvider(
            api_key=settings.sarvam_api_key,  # type: ignore[arg-type]  # sarvam_enabled guards None
            model=settings.sarvam_stt_model,
            base_url=settings.sarvam_base_url,
            mode=settings.sarvam_mode,
            timeout_s=settings.sarvam_timeout_s,
            fallback=provider,
        )
    return provider


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    aclose = getattr(_provider, "aclose", None)
    if aclose is not None:
        await aclose()


app = create_app(
    service_name="stt_svc",
    title="KOEL · STT_svc",
    description="Speech-to-text — Groq Whisper or deterministic stub.",
    lifespan=_lifespan,
)

_provider = _build_provider()
router = APIRouter(prefix="/v1", tags=["stt"])


@router.post("/transcribe", response_model=TranscriptResult)
async def transcribe(request: TranscribeRequest, _: ServicePrincipal) -> TranscriptResult:
    """Transcribe one audio chunk. Returns partial or final per ``is_final``."""
    with span("stt.transcribe", session_id=request.session_id, is_final=request.is_final):
        return await _provider.transcribe(request)


@router.post("/warmup")
async def warmup(_: ServicePrincipal) -> dict[str, bool]:
    """Prime provider connection pools ahead of a session's first turn (O-1).
    VB_Svc fires this at WS accept; best-effort (warm() never raises)."""
    await _provider.warm()
    return {"warmed": True}


app.include_router(router)
