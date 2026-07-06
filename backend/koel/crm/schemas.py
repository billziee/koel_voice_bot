"""Dummy CRM contracts. This is the interface a production CRM must satisfy."""

from __future__ import annotations

from pydantic import BaseModel, Field

from koel.db.models import UseCase


class CustomerRecord(BaseModel):
    id: str
    name: str
    phone: str
    language: str
    bank: str
    use_case: UseCase
    dnd: bool = False
    # Use-case-specific fields (offer_amount, product, dpd, emi, premium…).
    attributes: dict = Field(default_factory=dict)


class InteractionLog(BaseModel):
    """Appended per turn / per call (PRD §05 step 5 'CRM interaction log appended')."""

    customer_id: str
    session_id: str
    event: str  # e.g. "call_started", "promise_to_pay", "opt_out", "escalated"
    detail: dict = Field(default_factory=dict)
    # Set by the orchestrator so a retried write is deduped, not double-logged (AD).
    idempotency_key: str | None = Field(default=None, max_length=80)


class FlagRequest(BaseModel):
    """Flags driven by compliance/resilience flows (retry, opt-out, unsupported lang)."""

    customer_id: str
    flag: str  # "retry", "opt_out", "callback", "unsupported_language"
    reason: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class CallSummaryRequest(BaseModel):
    """Structured post-call insight (interest/objection/sentiment/next action),
    extracted by the voicebot's LLM summarizer and appended to the customer's
    CRM record. Deduped on session_id so a retry never double-writes."""

    session_id: str
    interest_shown: bool
    objection: str = ""
    sentiment: str = "neutral"  # "positive" | "neutral" | "negative"
    next_action: str = ""
