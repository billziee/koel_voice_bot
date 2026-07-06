"""Dummy CRM FastAPI app. Run: uvicorn koel.crm.main:app --port 8104

Richardson L2 resource endpoints over crm_customers. Reads/writes go through
the database so the same shape works when a real CRM replaces the seed data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.errors import ApiError
from koel.common.logging import get_logger, mask_account
from koel.common.telemetry import span
from koel.crm.schemas import CallSummaryRequest, CustomerRecord, FlagRequest, InteractionLog
from koel.db.models import CrmCustomer, InteractionLogRow
from koel.db.session import session_scope

logger = get_logger("koel.crm_svc")
app = create_app(
    service_name="crm_svc",
    title="KOEL · Dummy CRM",
    description="Synthetic customer records behind the production CRM interface contract.",
)
router = APIRouter(prefix="/v1/customers", tags=["crm"])


def _to_record(row: CrmCustomer) -> CustomerRecord:
    return CustomerRecord(
        id=row.id,
        name=row.name,
        phone=row.phone,
        language=row.language,
        bank=row.bank,
        use_case=row.use_case,
        dnd=row.dnd,
        attributes=row.attributes or {},
    )


@router.get("", response_model=list[CustomerRecord])
async def list_customers(_: ServicePrincipal) -> list[CustomerRecord]:
    async with session_scope() as db:
        rows = (await db.execute(select(CrmCustomer))).scalars().all()
    return [_to_record(r) for r in rows]


@router.get("/{customer_id}", response_model=CustomerRecord)
async def get_customer(customer_id: str, _: ServicePrincipal) -> CustomerRecord:
    with span("crm.get_customer", customer_id=customer_id):
        async with session_scope() as db:
            row = await db.get(CrmCustomer, customer_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message=f"No customer with id {customer_id}.",
            )
        return _to_record(row)


@router.post("/{customer_id}/interactions", status_code=status.HTTP_201_CREATED)
async def log_interaction(customer_id: str, payload: InteractionLog, _: ServicePrincipal) -> dict:
    """Persist a CRM interaction (AD). Idempotent: a retry with the same key writes once."""
    async with session_scope() as db:
        # Fast path: if this key was already recorded, return it unchanged.
        if payload.idempotency_key:
            existing = (
                await db.execute(
                    select(InteractionLogRow).where(
                        InteractionLogRow.idempotency_key == payload.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"status": "duplicate", "id": existing.id, "event": existing.event}
        row = InteractionLogRow(
            customer_id=customer_id,
            session_id=payload.session_id,
            event=payload.event,
            detail=payload.detail or {},
            idempotency_key=payload.idempotency_key,
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent retry beat us to the unique key — treat as a dedup, not an error.
            await db.rollback()
            return {"status": "duplicate", "event": payload.event}
        row_id = row.id
    logger.info(
        "crm_interaction",
        extra={"extra_fields": {"customer_id": mask_account(customer_id), "event": payload.event}},
    )
    return {"status": "logged", "id": row_id, "customer_id": customer_id, "event": payload.event}


@router.post("/{customer_id}/flags", status_code=status.HTTP_201_CREATED)
async def flag_customer(customer_id: str, payload: FlagRequest, _: ServicePrincipal) -> dict:
    """Set a compliance/retry flag (TRAI opt-out, retry, unsupported language)."""
    async with session_scope() as db:
        row = await db.get(CrmCustomer, customer_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message=f"No customer with id {customer_id}.",
            )
        attrs = dict(row.attributes or {})
        flags = set(attrs.get("flags", []))
        flags.add(payload.flag)
        attrs["flags"] = sorted(flags)
        if payload.flag == "opt_out":
            row.dnd = True
        row.attributes = attrs
    return {"status": "flagged", "customer_id": customer_id, "flag": payload.flag}


@router.post("/{customer_id}/call-summary", status_code=status.HTTP_201_CREATED)
async def add_call_summary(customer_id: str, payload: CallSummaryRequest, _: ServicePrincipal) -> dict:
    """Append the LLM-generated post-call summary onto the customer's record
    (visible via GET /v1/customers/{id}.attributes.call_notes). Deduped on
    session_id so a retry from the fire-and-forget caller never double-writes."""
    async with session_scope() as db:
        row = await db.get(CrmCustomer, customer_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message=f"No customer with id {customer_id}.",
            )
        attrs = dict(row.attributes or {})
        notes = list(attrs.get("call_notes", []))
        if any(n.get("session_id") == payload.session_id for n in notes):
            return {"status": "duplicate", "customer_id": customer_id, "session_id": payload.session_id}
        notes.append({
            "session_id": payload.session_id,
            "interest_shown": payload.interest_shown,
            "objection": payload.objection,
            "sentiment": payload.sentiment,
            "next_action": payload.next_action,
            "created_at": datetime.now(UTC).isoformat(),
        })
        attrs["call_notes"] = notes
        row.attributes = attrs
    logger.info(
        "crm_call_summary_added",
        extra={"extra_fields": {"customer_id": mask_account(customer_id), "session_id": payload.session_id}},
    )
    return {"status": "logged", "customer_id": customer_id, "session_id": payload.session_id}


app.include_router(router)
