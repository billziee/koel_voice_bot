"""TTS_svc FastAPI app. Run: uvicorn koel.tts.main:app --port 8103"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.tts.providers import StubOrpheusProvider
from koel.tts.schemas import SynthesisResult, SynthesizeRequest

app = create_app(
    service_name="tts_svc",
    title="KOEL · TTS_svc",
    description="Tone-aware neural TTS. Orpheus provider interface; stub provider for local demo.",
)

_provider = StubOrpheusProvider()
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
