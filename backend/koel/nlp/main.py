"""NLP_svc FastAPI app. Run: uvicorn koel.nlp.main:app --port 8102"""

from __future__ import annotations

from fastapi import APIRouter

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.telemetry import span
from koel.nlp.providers import StubNlpProvider
from koel.nlp.rag import HashEmbedder, retrieve
from koel.nlp.schemas import NlpResponse, TurnContext

app = create_app(
    service_name="nlp_svc",
    title="KOEL · NLP_svc",
    description="Intent classification, RAG-grounded response generation, tone tagging.",
)

_provider = StubNlpProvider()
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
