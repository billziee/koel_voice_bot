"""escalation requests (3.1): human-handoff queue

New table backing the real escalation handoff — the bot parks the call in
waiting_agent and writes the full context here for an operator to claim and
resolve. Additive; no change to existing tables.

Revision ID: b3d5e7a9c2f1
Revises: a7d3f2b9c1e4
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b3d5e7a9c2f1'
down_revision: str | Sequence[str] | None = 'a7d3f2b9c1e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = sa.Enum(
    "pending", "claimed", "resolved", "abandoned", name="escalationstatus"
)


def upgrade() -> None:
    op.create_table(
        "escalation_requests",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("language", sa.String(length=16), nullable=False, server_default="hinglish"),
        sa.Column("status", _STATUS, nullable=False, server_default="pending"),
        sa.Column("operator_sub", sa.String(length=64), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("crm_context", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_escalation_requests_org_id", "escalation_requests", ["org_id"]
    )
    op.create_index(
        "ix_escalation_requests_session_id", "escalation_requests", ["session_id"]
    )
    op.create_index(
        "ix_escalation_requests_status", "escalation_requests", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_escalation_requests_status", table_name="escalation_requests")
    op.drop_index("ix_escalation_requests_session_id", table_name="escalation_requests")
    op.drop_index("ix_escalation_requests_org_id", table_name="escalation_requests")
    op.drop_table("escalation_requests")
    _STATUS.drop(op.get_bind(), checkfirst=True)
