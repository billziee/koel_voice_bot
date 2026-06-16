"""NLP_svc FastAPI app. Run: uvicorn koel.nlp.main:app --port 8102"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.nlp.config import get_nlp_settings
from koel.nlp.providers import GroqNlpProvider, NlpProvider, StubNlpProvider
from koel.nlp.rag import HashEmbedder, retrieve
from koel.nlp.schemas import NlpResponse, TurnContext


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # Release the Groq HTTP client's connection pool on shutdown.
    aclose = getattr(_provider, "aclose", None)
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
            base_url=settings.groq_base_url,
            temperature=settings.groq_temperature,
            max_tokens=settings.groq_max_tokens,
            timeout_s=settings.groq_timeout_s,
        )
    return StubNlpProvider()


_provider = _build_provider()
_embedder = HashEmbedder()
router = APIRouter(prefix="/v1", tags=["nlp"])


@router.post("/respond", response_model=NlpResponse)
async def respond(ctx: TurnContext, _: ServicePrincipal) -> NlpResponse:
    """Classify intent, retrieve RAG context, generate the next bot turn."""
    retrieved = []
    if ctx.rag_enabled and ctx.rag_index:
        with span("nlp.rag_retrieve", session_id=ctx.session_id, rag_index=ctx.rag_index):
            try:
                retrieved = await retrieve(
                    ctx.customer_text, rag_index=ctx.rag_index, embedder=_embedder
                )
            except Exception:  # noqa: BLE001 — RAG is best-effort; degrade gracefully
                retrieved = []
    with span("nlp.generate", session_id=ctx.session_id, intent_known=bool(ctx.customer_text)):
        return await _provider.generate(ctx, retrieved)


app.include_router(router)
