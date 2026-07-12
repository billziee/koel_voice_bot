"""campaigns + dial attempts (1.2): outbound dialer queue

A campaign (bot + segment + retry policy) whose worker enqueues DialAttempt rows;
a worker claims due rows (pending + scheduled_at <= now) with FOR UPDATE SKIP
LOCKED and records each outcome, rescheduling retries per the campaign policy.
Also backs scheduled callbacks (2.5) via campaign-less attempts. Additive.

Revision ID: e2a9c4f60b17
Revises: d5f7091b3e64
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e2a9c4f60b17'
down_revision: str | Sequence[str] | None = 'd5f7091b3e64'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIAL_STATUS = sa.Enum(
    "pending", "in_progress", "done", "failed", name="dialattemptstatus"
)


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("retry_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_campaigns_org_id", "campaigns", ["org_id"])

    op.create_table(
        "dial_attempts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("campaign_id", sa.String(length=32), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "status", _DIAL_STATUS, nullable=False, server_default="pending"
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_outcome", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dial_attempts_org_id", "dial_attempts", ["org_id"])
    op.create_index("ix_dial_attempts_campaign_id", "dial_attempts", ["campaign_id"])
    op.create_index("ix_dial_attempts_status", "dial_attempts", ["status"])
    op.create_index("ix_dial_attempts_scheduled_at", "dial_attempts", ["scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_dial_attempts_scheduled_at", table_name="dial_attempts")
    op.drop_index("ix_dial_attempts_status", table_name="dial_attempts")
    op.drop_index("ix_dial_attempts_campaign_id", table_name="dial_attempts")
    op.drop_index("ix_dial_attempts_org_id", table_name="dial_attempts")
    op.drop_table("dial_attempts")
    op.drop_index("ix_campaigns_org_id", table_name="campaigns")
    op.drop_table("campaigns")
    _DIAL_STATUS.drop(op.get_bind(), checkfirst=True)
