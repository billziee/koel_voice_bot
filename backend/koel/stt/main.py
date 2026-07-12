"""STT_svc FastAPI app. Run: uvicorn koel.stt.main:app --port 8101"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.logging import get_logger
from koel.common.telemetry import span
from koel.stt.config import get_stt_settings
from koel.stt.providers import GroqWhisperProvider, SarvamSttProvider, SttProvider, StubWhisperProvider
from koel.stt.schemas import TranscribeRequest, TranscriptResult

logger = get_logger("koel.stt")


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
            stream_stt=settings.stream_stt,
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


def _enable_streaming_seam(provider: SttProvider) -> None:
    """Arm the WS transport on the provider that backs ``/v1/transcribe/stream``
    (O-6 activation).

    ``stream_stt`` (config) only ever gated ``SarvamSttProvider.stream_transcribe``
    — the dormant WS seam. Wiring the streaming endpoint IS that activation, so we
    flip the seam on here regardless of the config flag: a ``ws``-mode bot must
    attempt the verified WS path even in the default config. This touches ONLY
    ``stream_transcribe()``; the batch ``/v1/transcribe`` path calls
    ``transcribe()``, which never reads this flag, so ``chunk``-mode bots are
    completely unaffected. Providers without the seam (stub/Whisper) keep the
    batch-wrapping default and ignore this.
    """
    if isinstance(provider, SarvamSttProvider):
        provider._stream_stt = True  # noqa: SLF001 — endpoint-scoped seam activation


_enable_streaming_seam(_provider)
router = APIRouter(prefix="/v1", tags=["stt"])


@router.post("/transcribe", response_model=TranscriptResult)
async def transcribe(request: TranscribeRequest, _: ServicePrincipal) -> TranscriptResult:
    """Transcribe one audio chunk. Returns partial or final per ``is_final``."""
    with span("stt.transcribe", session_id=request.session_id, is_final=request.is_final):
        return await _provider.transcribe(request)


@router.post("/transcribe/stream")
async def transcribe_stream(request: TranscribeRequest, _: ServicePrincipal) -> StreamingResponse:
    """Stream NDJSON ``TranscriptResult`` frames for ``ws``-mode bots (O-6).

    Mirrors TTS_svc's ``/v1/stream`` NDJSON shape. Runs the provider's streaming
    seam (``SarvamSttProvider`` upgrades this to the verified ``/speech-to-text/ws``
    path; other providers wrap batch), yielding each result as one NDJSON line.
    The provider already degrades WS→batch internally; as a final guarantee, if the
    stream raises before a final (or yields none), we fall back to one batch
    ``transcribe()`` so the endpoint ALWAYS yields at least one final — a ws-mode
    turn is never dropped.
    """

    async def _gen():
        with span("stt.transcribe.stream", session_id=request.session_id, is_final=request.is_final):
            produced_final = False
            try:
                async for result in _provider.stream_transcribe(request):
                    produced_final = produced_final or result.is_final
                    yield result.model_dump_json() + "\n"
            except Exception:  # noqa: BLE001 — never drop a turn; degrade to batch
                logger.warning(
                    "stt_stream_failed; degrading to batch transcribe",
                    extra={"extra_fields": {"session_id": request.session_id}},
                    exc_info=True,
                )
            if not produced_final:
                result = await _provider.transcribe(request)
                if not result.is_final:
                    result = result.model_copy(update={"is_final": True})
                yield result.model_dump_json() + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.post("/warmup")
async def warmup(_: ServicePrincipal) -> dict[str, bool]:
    """Prime provider connection pools ahead of a session's first turn (O-1).
    VB_Svc fires this at WS accept; best-effort (warm() never raises)."""
    await _provider.warm()
    return {"warmed": True}


app.include_router(router)
