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
from koel.stt.providers import (
    DeepgramSttProvider,
    GroqWhisperProvider,
    SarvamSttProvider,
    SttProvider,
    StubWhisperProvider,
)
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
    # Deepgram is the STREAMING provider only when explicitly selected AND keyed
    # (both default off → nothing changes for existing deployments). It WRAPS the
    # provider built above as its batch fallback: DeepgramSttProvider.transcribe()
    # delegates straight to that fallback, so the batch /v1/transcribe path and
    # chunk-mode bots are byte-for-byte unaffected — only stream_transcribe()
    # upgrades to Deepgram's genuine interim partials (O-6). When Deepgram is not
    # selected, the Sarvam WS seam (armed in _enable_streaming_seam) is used as before.
    if settings.stream_stt_provider.lower() == "deepgram" and settings.deepgram_enabled:
        provider = DeepgramSttProvider(
            api_key=settings.deepgram_api_key,  # type: ignore[arg-type]  # deepgram_enabled guards None
            model=settings.deepgram_model,
            base_url=settings.deepgram_base_url,
            fallback=provider,
        )
    return provider


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
    service_name="stt_svc",
    title="KOEL · STT_svc",
    description="Speech-to-text — Groq Whisper or deterministic stub.",
    lifespan=_lifespan,
)

_settings = get_stt_settings()
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

    ``DeepgramSttProvider`` (selected via ``stream_stt_provider="deepgram"``) has no
    dormant flag — its ``stream_transcribe`` is always armed — so it is not a
    ``SarvamSttProvider`` and this is a no-op for it.
    """
    if isinstance(provider, SarvamSttProvider):
        provider._stream_stt = True  # noqa: SLF001 — endpoint-scoped seam activation


_enable_streaming_seam(_provider)
router = APIRouter(prefix="/v1", tags=["stt"])

# BYOK per-connection STT providers (Phase 3), cached by a signature of the
# resolved connection so an edit (new key/url/model/params) rebuilds instead of
# serving a stale client. Bounded by the number of distinct custom connections.
_conn_providers: dict[str, tuple[str, SttProvider]] = {}


def _conn_signature(resolved) -> str:  # noqa: ANN001 — ResolvedConnection
    p = resolved.params or {}
    return (
        f"{resolved.provider}|{resolved.base_url}|{resolved.model}|{hash(resolved.api_key)}"
        f"|{p.get('mode')}"
    )


def _build_conn_provider(resolved) -> SttProvider | None:  # noqa: ANN001 — ResolvedConnection
    """Build a BYOK STT provider from a resolved connection, keyed by its
    ``provider`` label. Returns None for an unknown label so the caller degrades
    to the service default. Each provider wraps the default as its own batch
    fallback, so ``/transcribe`` stays authoritative and a live turn is never
    dropped. The streaming seam is armed (Sarvam WS / Deepgram partials) so a
    ws-mode bot's connection wins over the env default when set."""
    params = resolved.params or {}
    label = (resolved.provider or "").lower()
    if label == "deepgram":
        return DeepgramSttProvider(
            api_key=resolved.api_key,
            model=resolved.model or _settings.deepgram_model,
            base_url=resolved.base_url or _settings.deepgram_base_url,
            fallback=_provider,
            timeout_s=_settings.sarvam_timeout_s,
        )
    if label == "sarvam":
        return SarvamSttProvider(
            api_key=resolved.api_key,
            model=resolved.model or _settings.sarvam_stt_model,
            base_url=resolved.base_url or _settings.sarvam_base_url,
            mode=str(params.get("mode", _settings.sarvam_mode)),
            timeout_s=_settings.sarvam_timeout_s,
            fallback=_provider,
            stream_stt=True,  # endpoint-scoped WS seam activation (mirrors _enable_streaming_seam)
        )
    return None


async def _provider_for(request: TranscribeRequest) -> SttProvider:
    """The STT provider for this request: a per-bot BYOK connection when set, else
    the service default. BYOK never breaks a turn — any resolve/build failure (no
    key, inactive, wrong kind, unknown provider, undecryptable, secrets disabled)
    falls back to the default."""
    if not request.stt_connection_id:
        return _provider
    try:
        from koel.common.model_connections import resolve_connection

        resolved = await resolve_connection(request.stt_connection_id)
        if resolved is None or resolved.kind != "stt":
            return _provider
        sig = _conn_signature(resolved)
        cached = _conn_providers.get(request.stt_connection_id)
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
        _conn_providers[request.stt_connection_id] = (sig, provider)
        return provider
    except Exception:  # noqa: BLE001 — BYOK is best-effort; never break the turn
        logger.warning("byok_stt_provider_failed conn=%s; using default", request.stt_connection_id)
        return _provider


@router.post("/transcribe", response_model=TranscriptResult)
async def transcribe(request: TranscribeRequest, _: ServicePrincipal) -> TranscriptResult:
    """Transcribe one audio chunk. Returns partial or final per ``is_final``."""
    provider = await _provider_for(request)  # BYOK per-bot STT connection or the default
    with span("stt.transcribe", session_id=request.session_id, is_final=request.is_final):
        return await provider.transcribe(request)


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
    provider = await _provider_for(request)  # BYOK per-bot STT connection or the default

    async def _gen():
        with span("stt.transcribe.stream", session_id=request.session_id, is_final=request.is_final):
            produced_final = False
            try:
                async for result in provider.stream_transcribe(request):
                    produced_final = produced_final or result.is_final
                    yield result.model_dump_json() + "\n"
            except Exception:  # noqa: BLE001 — never drop a turn; degrade to batch
                logger.warning(
                    "stt_stream_failed; degrading to batch transcribe",
                    extra={"extra_fields": {"session_id": request.session_id}},
                    exc_info=True,
                )
            if not produced_final:
                result = await provider.transcribe(request)
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
