"""consent records (2.3): evidentiary verbal-consent trail

Links the transcript turn that affirmed consent to the recording + disclosure
version. Additive; written only for consent-required bots.

Revision ID: d5f7091b3e64
Revises: c4e6f8b1d3a2
Create Date: 2026-07-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd5f7091b3e64'
down_revision: str | Sequence[str] | None = 'c4e6f8b1d3a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent_records",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("call_id", sa.String(length=32), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("disclosure_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("turn_index", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("consent_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("recording_url", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_consent_records_org_id", "consent_records", ["org_id"])
    op.create_index("ix_consent_records_call_id", "consent_records", ["call_id"])
    op.create_index("ix_consent_records_session_id", "consent_records", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_consent_records_session_id", table_name="consent_records")
    op.drop_index("ix_consent_records_call_id", table_name="consent_records")
    op.drop_index("ix_consent_records_org_id", table_name="consent_records")
    op.drop_table("consent_records")
