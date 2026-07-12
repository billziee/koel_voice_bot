"""NLP_svc FastAPI app. Run: uvicorn koel.nlp.main:app --port 8102"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.responses import StreamingResponse

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.logging import get_logger
from koel.common.schemas import Tone
from koel.common.telemetry import span
from koel.nlp.config import get_nlp_settings
from koel.nlp.providers import GroqNlpProvider, NlpProvider, SarvamNlpProvider, StubNlpProvider
from koel.nlp.rag import build_embedder, retrieve
from koel.nlp.schemas import Intent, NlpResponse, NlpStreamFrame, RetrievedChunk, TurnContext

logger = get_logger("koel.nlp_svc")


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # Release provider + embedder HTTP client pools on shutdown.
    for obj in (_provider, _embedder):
        aclose = getattr(obj, "aclose", None)
        if aclose is not None:
            await aclose()


app = create_app(
    service_name="nlp_svc",
    title="KOEL · NLP_svc",
    description="Intent classification, RAG-grounded response generation, tone tagging.",
    lifespan=_lifespan,
)


def _build_provider() -> NlpProvider:
    settings = get_nlp_settings()
    if settings.sarvam_nlp_enabled:
        # O-7 Track A: India-hosted NLP. Reuses the Groq JSON-mode contract.
        return SarvamNlpProvider(
            api_key=settings.sarvam_api_key,  # type: ignore[arg-type]  # sarvam_nlp_enabled guards None
            model=settings.sarvam_model,
            base_url=settings.sarvam_base_url,
            hinglish_script=settings.hinglish_script,
            temperature=settings.groq_temperature,
            max_tokens=settings.groq_max_tokens,
            timeout_s=settings.groq_timeout_s,
        )
    if settings.groq_enabled:
        return GroqNlpProvider(
            api_key=settings.groq_api_key,  # type: ignore[arg-type]  # groq_enabled guards None
            model=settings.groq_model,
            model_fast=settings.groq_model_fast,
            model_quality=settings.groq_model_quality,
            hinglish_script=settings.hinglish_script,
            base_url=settings.groq_base_url,
            temperature=settings.groq_temperature,
            max_tokens=settings.groq_max_tokens,
            timeout_s=settings.groq_timeout_s,
        )
    return StubNlpProvider()


_settings = get_nlp_settings()
_provider = _build_provider()
_embedder = build_embedder(_settings)
router = APIRouter(prefix="/v1", tags=["nlp"])


async def _retrieve_for(ctx: TurnContext) -> list[RetrievedChunk]:
    """Best-effort RAG retrieval for a turn (shared by the batch + stream routes)."""
    if not (ctx.rag_enabled and ctx.rag_index):
        return []
    with span("nlp.rag_retrieve", session_id=ctx.session_id, rag_index=ctx.rag_index):
        try:
            # Per-bot overrides (W) win over the service defaults when set.
            top_k = ctx.rag_top_k if ctx.rag_top_k is not None else _settings.rag_top_k
            threshold = (
                ctx.rag_score_threshold
                if ctx.rag_score_threshold is not None
                else _settings.rag_score_threshold
            )
            return await retrieve(
                ctx.customer_text,
                rag_index=ctx.rag_index,
                embedder=_embedder,
                top_k=top_k,
                score_threshold=threshold,
                org_id=ctx.org_id,
            )
        except Exception:  # noqa: BLE001 — RAG is best-effort; degrade gracefully
            return []


@router.post("/retrieve", response_model=list[RetrievedChunk])
async def retrieve_route(ctx: TurnContext, _: ServicePrincipal) -> list[RetrievedChunk]:
    """Speculative RAG retrieval (O-5): VB_Svc pre-fetches the KB chunks for a
    held transcript DURING the endpointing hold window, so they are already in
    hand when the turn runs (hiding retrieval latency). Same body shape + auth as
    ``/respond``; RAG-disabled or an empty index ⇒ ``[]`` (degrade-safe)."""
    with span("nlp.retrieve", session_id=ctx.session_id, rag_index=ctx.rag_index):
        return await _retrieve_for(ctx)


@router.post("/respond", response_model=NlpResponse)
async def respond(ctx: TurnContext, _: ServicePrincipal) -> NlpResponse:
    """Classify intent, retrieve RAG context, generate the next bot turn."""
    # O-5: honour chunks VB_Svc pre-fetched during the hold window — using them
    # skips a second retrieval. Empty ⇒ retrieve now, exactly as before.
    retrieved = ctx.retrieved or await _retrieve_for(ctx)
    with span("nlp.generate", session_id=ctx.session_id, intent_known=bool(ctx.customer_text)):
        return await _provider.generate(ctx, retrieved)


@router.post("/respond/stream")
async def respond_stream(ctx: TurnContext, _: ServicePrincipal) -> StreamingResponse:
    """First-token streaming (O-2): NDJSON of sentence frames as the reply forms,
    then a terminal ``final`` frame with the full NlpResponse. VB_Svc feeds each
    sentence into sentence-streaming TTS as it arrives instead of waiting for the
    whole completion; the batch /respond route stays the fallback."""
    # O-5: use VB_Svc's pre-fetched chunks when present; else retrieve now.
    retrieved = ctx.retrieved or await _retrieve_for(ctx)

    async def _gen():
        with span("nlp.generate_stream", session_id=ctx.session_id):
            emitted_final = False
            try:
                async for frame in _provider.generate_stream(ctx, retrieved):
                    emitted_final = emitted_final or frame.type == "final"
                    yield frame.model_dump_json() + "\n"
            except Exception:  # noqa: BLE001 — the consumer's contract needs a terminal frame
                logger.warning(
                    "respond_stream_failed", extra={"session_id": ctx.session_id}, exc_info=True
                )
            if not emitted_final:
                # Guarantee a terminal frame so VB_Svc never hangs waiting for one.
                # Mark it escalate so a genuinely-degraded turn hands off to a human
                # rather than continuing on an unknown intent (mirrors the orchestrator's
                # reconcile-failure path; a dropped opt-out must never be silent).
                yield NlpStreamFrame(
                    type="final",
                    response=NlpResponse(
                        session_id=ctx.session_id,
                        response_text="",
                        intent=Intent.unknown,
                        tone=Tone.neutral,
                        escalate=True,
                        escalation_reason="nlp stream degraded — no terminal frame",
                    ),
                ).model_dump_json() + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.post("/warmup")
async def warmup(_: ServicePrincipal) -> dict[str, bool]:
    """Prime provider connection pools ahead of a session's first turn (O-1).
    VB_Svc fires this at WS accept; best-effort (warm() never raises)."""
    await _provider.warm()
    return {"warmed": True}


app.include_router(router)
