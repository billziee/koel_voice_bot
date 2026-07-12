"""NLP_svc contracts — the structured output the orchestrator depends on."""

from __future__ import annotations

from enum import Enum
from typing import Literal

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


class Sentiment(str, Enum):
    """Customer sentiment for the current turn (PRD §06). Drives in-call tone
    adaptation (roadmap 4.3): a sustained negative trend softens the bot's tone."""

    positive = "positive"
    neutral = "neutral"
    negative = "negative"


class TurnContext(BaseModel):
    """One customer turn handed to NLP_svc, with the context VB_Svc has gathered."""

    session_id: str
    bot_id: str
    org_id: str | None = None  # tenant scope for RAG retrieval isolation (AF)
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
    # Per-bot NLP overrides (roadmap 5.4); None → NLP_svc config defaults.
    # prompt_preamble is added to (never replaces) the hardened system prompt.
    llm_model: str | None = None
    llm_temperature: float | None = None
    prompt_preamble: str | None = None


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
    # The CUSTOMER's sentiment this turn (roadmap 4.3). Defaulted so stub/older
    # responses stay valid; the orchestrator maps a rolling trend → next-turn tone.
    customer_sentiment: Sentiment = Sentiment.neutral
    escalate: bool = False
    escalation_reason: str | None = None
    end_call: bool = False
    # Structured in-call actions the model requests (2.1): e.g.
    # {"type": "send_payment_link", "amount": 4500} / {"type": "promise_to_pay",
    # "amount": 4500, "date": "2026-07-20"}. The orchestrator dispatches them
    # deterministically after the turn; unknown/malformed entries are ignored.
    actions: list[dict] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    # KB chunks the reply cited; empty when the turn asserts no product fact.
    citations: list[Citation] = Field(default_factory=list)
    # False when a product-fact assertion could not be tied to a citation — the
    # orchestrator/eval can treat this as a soft grounding failure (never hard-drop).
    grounded: bool = True


class NlpStreamFrame(BaseModel):
    """One NDJSON frame from ``POST /v1/respond/stream`` (roadmap O-2).

    ``sentence`` frames carry a complete reply sentence for immediate TTS as it's
    generated; the terminal ``final`` frame carries the full ``NlpResponse`` for
    intent/escalation/state. Concatenating the ``sentence`` frames always equals
    the final ``response_text`` — the stream never diverges from the batch parse.
    """

    type: Literal["sentence", "final"]
    text: str | None = None
    response: NlpResponse | None = None
