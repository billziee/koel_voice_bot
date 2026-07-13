"""model connections (BYOK): custom LLM/TTS/STT endpoints

Org-scoped reusable connections to customer-supplied model providers. The
api_key rides in api_key_encrypted (Fernet, never plaintext). Additive.

Revision ID: b7c2e9d4a1f8
Revises: a4d1f7e2c9b6
Create Date: 2026-07-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b7c2e9d4a1f8'
down_revision: str | Sequence[str] | None = 'a4d1f7e2c9b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_connections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="openai_compatible"),
        sa.Column("base_url", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("api_key_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_connections_org_id", "model_connections", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_model_connections_org_id", table_name="model_connections")
    op.drop_table("model_connections")
