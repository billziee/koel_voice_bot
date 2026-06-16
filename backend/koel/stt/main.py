"""STT_svc FastAPI app. Run: uvicorn koel.stt.main:app --port 8101"""

from __future__ import annotations

from fastapi import APIRouter

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.stt.providers import StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult

app = create_app(
    service_name="stt_svc",
    title="KOEL · STT_svc",
    description="Speech-to-text. Whisper provider interface; stub provider for local demo.",
)

_provider = StubWhisperProvider()
router = APIRouter(prefix="/v1", tags=["stt"])


@router.post("/transcribe", response_model=TranscriptResult)
async def transcribe(request: TranscribeRequest, _: ServicePrincipal) -> TranscriptResult:
    """Transcribe one audio chunk. Returns partial or final per ``is_final``."""
    with span("stt.transcribe", session_id=request.session_id, is_final=request.is_final):
        return await _provider.transcribe(request)


app.include_router(router)
