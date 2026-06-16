"""Dummy CRM FastAPI app. Run: uvicorn koel.crm.main:app --port 8104

Richardson L2 resource endpoints over crm_customers. Reads/writes go through
the database so the same shape works when a real CRM replaces the seed data.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select

from koel.common.app_factory import create_app
from koel.common.auth import ServicePrincipal
from koel.common.errors import ApiError
from koel.common.logging import get_logger, mask_account
from koel.common.telemetry import span
from koel.crm.schemas import CustomerRecord, FlagRequest, InteractionLog
from koel.db.models import CrmCustomer
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
    # MVP: interaction logs are recorded on the call log in PostgreSQL by VB_Svc;
    # here we surface the contract + emit a business-event log line (PRD §08).
    logger.info(
        "crm_interaction",
        extra={"extra_fields": {"customer_id": mask_account(customer_id), "event": payload.event}},
    )
    return {"status": "logged", "customer_id": customer_id, "event": payload.event}


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


app.include_router(router)
