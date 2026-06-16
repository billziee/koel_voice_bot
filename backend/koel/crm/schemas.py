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


class FlagRequest(BaseModel):
    """Flags driven by compliance/resilience flows (retry, opt-out, unsupported lang)."""

    customer_id: str
    flag: str  # "retry", "opt_out", "callback", "unsupported_language"
    reason: str | None = None
