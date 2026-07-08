"""NLP_svc FastAPI app. Run: uvicorn koel.nlp.main:app --port 8102"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.nlp.config import get_nlp_settings
from koel.nlp.providers import GroqNlpProvider, NlpProvider, StubNlpProvider
from koel.nlp.rag import build_embedder, retrieve
from koel.nlp.schemas import NlpResponse, TurnContext


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


@router.post("/respond", response_model=NlpResponse)
async def respond(ctx: TurnContext, _: ServicePrincipal) -> NlpResponse:
    """Classify intent, retrieve RAG context, generate the next bot turn."""
    retrieved = []
    if ctx.rag_enabled and ctx.rag_index:
        with span("nlp.rag_retrieve", session_id=ctx.session_id, rag_index=ctx.rag_index):
            try:
                # Per-bot overrides (W) win over the service defaults when set.
                top_k = ctx.rag_top_k if ctx.rag_top_k is not None else _settings.rag_top_k
                threshold = (
                    ctx.rag_score_threshold
                    if ctx.rag_score_threshold is not None
                    else _settings.rag_score_threshold
                )
                retrieved = await retrieve(
                    ctx.customer_text,
                    rag_index=ctx.rag_index,
                    embedder=_embedder,
                    top_k=top_k,
                    score_threshold=threshold,
                    org_id=ctx.org_id,
                )
            except Exception:  # noqa: BLE001 — RAG is best-effort; degrade gracefully
                retrieved = []
    with span("nlp.generate", session_id=ctx.session_id, intent_known=bool(ctx.customer_text)):
        return await _provider.generate(ctx, retrieved)


@router.post("/warmup")
async def warmup(_: ServicePrincipal) -> dict[str, bool]:
    """Prime provider connection pools ahead of a session's first turn (O-1).
    VB_Svc fires this at WS accept; best-effort (warm() never raises)."""
    await _provider.warm()
    return {"warmed": True}


app.include_router(router)
