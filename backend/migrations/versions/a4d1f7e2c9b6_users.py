"""users table (6.4 follow-up): per-login org scoping

Adds the ``users`` table that maps a login identity to an org + role. Login
(``koel.voicebot.api.auth``) now looks the user up by username, verifies the
bcrypt ``password_hash``, and mints a token carrying that user's ``org_id`` +
``role`` — so a logged-in operator is scoped to their tenant instead of the
unscoped "sees all" principal every prior login minted.

This is ADDITIVE: it introduces a new table and touches no existing data.
``org_id`` is nullable — ``NULL`` is the platform-admin / single-tenant
"sees all" principal, the same convention every scoped table already uses.

Revision ID: a4d1f7e2c9b6
Revises: f3b8c1d0a2e5
Create Date: 2026-07-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a4d1f7e2c9b6'
down_revision: str | Sequence[str] | None = 'f3b8c1d0a2e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.LargeBinary(), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="operator"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
