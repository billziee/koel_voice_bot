"""NLP_svc contracts — the structured output the orchestrator depends on."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from koel.common.schemas import Language, Tone


class Intent(str, Enum):
    """Intent classes (PRD §05 step 4, §04 sample dialogue)."""

    high_interest = "high_interest"
    interested = "interested"
    objection = "objection"
    not_interested = "not_interested"
    busy = "busy"  # "call me later / I'm busy" — offer a callback, don't pitch
    promise_to_pay = "promise_to_pay"
    dispute = "dispute"
    request_human = "request_human"
    opt_out = "opt_out"  # TRAI opt-out (PRD §11) — flags CRM + ends call
    smalltalk = "smalltalk"
    unknown = "unknown"


class TurnContext(BaseModel):
    """One customer turn handed to NLP_svc, with the context VB_Svc has gathered."""

    session_id: str
    bot_id: str
    use_case: str
    language: Language
    customer_text: str
    # Rolling history: [{"speaker": "bot"|"customer", "text": ...}, ...]
    history: list[dict] = Field(default_factory=list)
    # CRM fields seeded into the session (name, product, offer, DPD…).
    crm_context: dict = Field(default_factory=dict)
    rag_enabled: bool = True
    rag_index: str | None = None
    # Per-bot RAG tuning overrides (W); None → NLP_svc config defaults.
    rag_top_k: int | None = None
    rag_score_threshold: float | None = None


class RetrievedChunk(BaseModel):
    title: str
    content: str
    score: float


class Citation(BaseModel):
    """A KB chunk the reply grounded a product fact in (PRD §12 anti-hallucination)."""

    title: str
    excerpt: str = ""


class NlpResponse(BaseModel):
    """Structured turn output. ``tone`` is always present (PRD §14 schema validation)."""

    session_id: str
    response_text: str
    intent: Intent
    tone: Tone
    escalate: bool = False
    escalation_reason: str | None = None
    end_call: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    # KB chunks the reply cited; empty when the turn asserts no product fact.
    citations: list[Citation] = Field(default_factory=list)
    # False when a product-fact assertion could not be tied to a citation — the
    # orchestrator/eval can treat this as a soft grounding failure (never hard-drop).
    grounded: bool = True
