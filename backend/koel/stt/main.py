"""STT_svc FastAPI app. Run: uvicorn koel.stt.main:app --port 8101"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.stt.config import get_stt_settings
from koel.stt.providers import GroqWhisperProvider, SttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult


def _build_provider() -> SttProvider:
    settings = get_stt_settings()
    if settings.groq_enabled:
        return GroqWhisperProvider(
            api_key=settings.groq_api_key,  # type: ignore[arg-type]  # groq_enabled guards None
            model=settings.groq_whisper_model,
            base_url=settings.groq_base_url,
            timeout_s=settings.groq_timeout_s,
        )
    return StubWhisperProvider()


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


app.include_router(router)
