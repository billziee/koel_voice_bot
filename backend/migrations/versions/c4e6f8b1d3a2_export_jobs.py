"""export jobs (6.5): regulatory audit-export trail

Records each regulatory export (who/what-range/how-many/manifest-hash/where).
Additive; no change to existing tables.

Revision ID: c4e6f8b1d3a2
Revises: b3d5e7a9c2f1
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c4e6f8b1d3a2'
down_revision: str | Sequence[str] | None = 'b3d5e7a9c2f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("object_url", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_export_jobs_org_id", "export_jobs", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_export_jobs_org_id", table_name="export_jobs")
    op.drop_table("export_jobs")
