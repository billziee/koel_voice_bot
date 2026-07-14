"""Tenant (organization) registry admin API (6.4 Multi-tenant enablement).

Org scoping is already pervasive — every table carries a nullable indexed
``org_id`` and the read/list routers filter by the caller's org (AF). What was
missing to actually *enable* multi-tenancy is an authoritative registry of the
tenants those ids point at. This module exposes it as an admin-only CRUD surface.

Admin-vs-tenant gating (discovered, not invented): the whole codebase treats a
platform-level *unscoped* principal (``org_id is None`` → "sees all", the admin
view used by every scoped router) as distinct from a *tenant-scoped* principal
(``org_id`` set → sees only its own org). Managing the registry is a
platform-level action, so it requires an unscoped principal; a tenant-scoped
token is forbidden (403). The gate is applied at the router level so every route
inherits it. Follows the shared ApiError envelope + response conventions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from koel.common.auth import CurrentUser, TokenClaims
from koel.common.errors import ApiError
from koel.common.logging import get_logger
from koel.db.models import Org
from koel.db.session import session_scope

logger = get_logger("koel.voicebot.orgs")


async def require_org_admin(user: CurrentUser) -> TokenClaims:
    """Only a platform-level (unscoped) principal may manage the org registry.

    Mirrors how every scoped router treats ``org_id is None`` as the admin /
    "sees all" view: a tenant-scoped operator (``org_id`` set) manages only its
    own org's resources and must never create/mutate the tenant registry itself.
    """
    if user.org_id is not None:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="Managing organizations requires a platform admin (unscoped) token.",
        )
    return user


OrgAdmin = Annotated[TokenClaims, Depends(require_org_admin)]

router = APIRouter(prefix="/v1/orgs", tags=["orgs"], dependencies=[Depends(require_org_admin)])


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # URL-safe tenant handle; validated at the boundary so a bad slug fails 422
    # rather than landing in the registry.
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrgOut(BaseModel):
    id: str
    name: str
    slug: str
    active: bool
    created_at: str | None = None


def _to_out(row: Org) -> OrgOut:
    return OrgOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        active=row.active,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


@router.get("", response_model=list[OrgOut])
async def list_orgs() -> list[OrgOut]:
    """All registered tenants, oldest first (admin only)."""
    async with session_scope() as db:
        rows = (await db.execute(select(Org).order_by(Org.created_at.asc()))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(payload: OrgIn, admin: OrgAdmin) -> OrgOut:
    """Register a new tenant. The slug is unique — a duplicate is rejected 409."""
    async with session_scope() as db:
        existing = (
            await db.execute(select(Org).where(Org.slug == payload.slug))
        ).scalars().first()
        if existing is not None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code="org_slug_exists",
                message=f"An organization with slug '{payload.slug}' already exists.",
            )
        row = Org(name=payload.name, slug=payload.slug)
        db.add(row)
        await db.flush()
        out = _to_out(row)
    logger.info("org_created", extra={"extra_fields": {"org_id": out.id, "slug": out.slug, "by": admin.sub}})
    return out


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(org_id: str) -> OrgOut:
    """A single tenant by id (admin only). 404 if it doesn't exist."""
    async with session_scope() as db:
        row = await db.get(Org, org_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="org_not_found",
                message=f"No organization with id {org_id}.",
            )
        out = _to_out(row)
    return out


@router.post("/{org_id}/deactivate", response_model=OrgOut)
async def deactivate_org(org_id: str, admin: OrgAdmin) -> OrgOut:
    """Soft-disable a tenant (``active=False``) without deleting its data. 404 if
    the tenant doesn't exist. Idempotent — deactivating an inactive org is a no-op."""
    async with session_scope() as db:
        row = await db.get(Org, org_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="org_not_found",
                message=f"No organization with id {org_id}.",
            )
        row.active = False
        await db.flush()
        out = _to_out(row)
    logger.info("org_deactivated", extra={"extra_fields": {"org_id": org_id, "by": admin.sub}})
    return out
