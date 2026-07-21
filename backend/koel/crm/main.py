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
from koel.crm.adapters import mirror_to_webhook
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


def _normalize_phone(phone: str) -> str:
    """Canonicalise a phone number to bare E.164-ish digits for exact-match ANI
    lookup: strip spaces/dashes/parens/dots and map a leading '00' to '+'. A real
    dialer ANI arrives as '+919000000001'; a hand-entered '+91-90000-00001' or
    '+91 90000 00001' normalises to the same string so the lookup resolves."""
    if not phone:
        return ""
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned


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
async def list_customers(_: ServicePrincipal, org_id: str | None = None) -> list[CustomerRecord]:
    """List customers. Org-scoped like the other customer routes (AF): with
    ``org_id`` set, only that tenant's records are returned so the list can't leak
    another org's customer PII."""
    stmt = select(CrmCustomer).order_by(CrmCustomer.id.asc())
    if org_id is not None:
        stmt = stmt.where(CrmCustomer.org_id == org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [_to_record(r) for r in rows]


@router.get("/by-phone", response_model=CustomerRecord)
async def get_customer_by_phone(
    phone: str, _: ServicePrincipal, org_id: str | None = None
) -> CustomerRecord:
    """ANI lookup for inbound calls (roadmap 3.2): resolve a caller by their phone
    number. Declared before ``/{customer_id}`` so the literal path wins over the
    dynamic one. Org-scoped like the other customer routes — when ``org_id`` is
    given, only that tenant's records match (multi-tenant isolation, AF)."""
    with span("crm.get_customer_by_phone"):
        # Deterministic resolution: phone has no unique constraint (a real CRM
        # enforces it; the seed may not), so order by id and take the lowest so the
        # SAME caller always resolves to the SAME record — never an arbitrary row.
        stmt = (
            select(CrmCustomer)
            .where(CrmCustomer.phone == _normalize_phone(phone))
            .order_by(CrmCustomer.id.asc())
        )
        if org_id is not None:
            stmt = stmt.where(CrmCustomer.org_id == org_id)
        async with session_scope() as db:
            rows = (await db.execute(stmt)).scalars().all()
        if len(rows) > 1:
            logger.warning(
                "crm_ambiguous_phone_lookup",
                extra={"extra_fields": {"phone": mask_account(phone), "matches": len(rows)}},
            )
        row = rows[0] if rows else None
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message="No customer with that phone number.",
            )
        return _to_record(row)


@router.get("/{customer_id}", response_model=CustomerRecord)
async def get_customer(
    customer_id: str, _: ServicePrincipal, org_id: str | None = None
) -> CustomerRecord:
    """Fetch one customer. Org-scoped like the other customer routes (AF): when
    ``org_id`` is given, a record from a different tenant 404s exactly like a
    missing one — so the caller cannot read another org's customer PII, and the
    record's existence isn't leaked cross-tenant."""
    with span("crm.get_customer", customer_id=customer_id):
        async with session_scope() as db:
            row = await db.get(CrmCustomer, customer_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="customer_not_found",
                message=f"No customer with id {customer_id}.",
            )
        return _to_record(row)


@router.post("/{customer_id}/interactions", status_code=status.HTTP_201_CREATED)
async def log_interaction(customer_id: str, payload: InteractionLog, _: ServicePrincipal) -> dict:
    """Persist a CRM interaction (AD). Idempotent: a retry with the same key writes once."""
    if payload.customer_id and payload.customer_id != customer_id:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="customer_id_mismatch",
            message="Body customer_id does not match the path.",
        )
    async with session_scope() as db:
        # Fast path: if this key was already recorded FOR THIS CUSTOMER, return it
        # unchanged. Scoped by customer_id so a reused key never returns (or leaks
        # the id/event of) a DIFFERENT customer's interaction row.
        if payload.idempotency_key:
            existing = (
                await db.execute(
                    select(InteractionLogRow).where(
                        InteractionLogRow.idempotency_key == payload.idempotency_key,
                        InteractionLogRow.customer_id == customer_id,
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
    await mirror_to_webhook("interaction", customer_id, payload.model_dump())  # 6.1: dormant w/o webhook
    return {"status": "logged", "id": row_id, "customer_id": customer_id, "event": payload.event}


@router.post("/{customer_id}/flags", status_code=status.HTTP_201_CREATED)
async def flag_customer(customer_id: str, payload: FlagRequest, _: ServicePrincipal) -> dict:
    """Set a compliance/retry flag (TRAI opt-out, retry, unsupported language)."""
    async with session_scope() as db:
        # FOR UPDATE: this is a read-modify-write on the JSON attributes column, so
        # lock the row for the txn — otherwise a concurrent flag/summary write reads
        # the same attrs and the last writer clobbers the other's change (lost flag).
        # (No-op on SQLite tests; enforced on Postgres.)
        row = (
            await db.execute(
                select(CrmCustomer).where(CrmCustomer.id == customer_id).with_for_update()
            )
        ).scalar_one_or_none()
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
    await mirror_to_webhook("flag", customer_id, payload.model_dump())  # 6.1: dormant w/o webhook
    return {"status": "flagged", "customer_id": customer_id, "flag": payload.flag}


@router.post("/{customer_id}/call-summary", status_code=status.HTTP_201_CREATED)
async def add_call_summary(customer_id: str, payload: CallSummaryRequest, _: ServicePrincipal) -> dict:
    """Append the LLM-generated post-call summary onto the customer's record
    (visible via GET /v1/customers/{id}.attributes.call_notes). Deduped on
    session_id so a retry from the fire-and-forget caller never double-writes."""
    async with session_scope() as db:
        # FOR UPDATE: read-modify-write on the JSON attributes column — lock the row
        # so a concurrent flag/summary write can't clobber this append (see
        # flag_customer). No-op on SQLite tests; enforced on Postgres.
        row = (
            await db.execute(
                select(CrmCustomer).where(CrmCustomer.id == customer_id).with_for_update()
            )
        ).scalar_one_or_none()
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
    await mirror_to_webhook("call_summary", customer_id, payload.model_dump())  # 6.1: dormant w/o webhook
    return {"status": "logged", "customer_id": customer_id, "session_id": payload.session_id}


app.include_router(router)
