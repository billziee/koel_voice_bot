"""orgs registry (6.4): tenant registry to enable multi-tenancy

Adds the authoritative ``orgs`` table backing the nullable ``org_id`` that every
scoped table already carries. This is ADDITIVE and deliberately NON-destructive:
existing tables' ``org_id`` stays nullable and is NOT backfilled or tightened to
NOT NULL — single-tenant pilot rows legitimately keep ``org_id=NULL``. Enabling
multi-tenancy here means adding the registry (plus the cross-tenant isolation
gate proven in the test suite), not rewriting live data.

Revision ID: f3b8c1d0a2e5
Revises: e2a9c4f60b17
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'f3b8c1d0a2e5'
down_revision: str | Sequence[str] | None = 'e2a9c4f60b17'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_orgs_slug", "orgs", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_orgs_slug", table_name="orgs")
    op.drop_table("orgs")
