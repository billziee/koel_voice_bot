"""google sign-in: passwordless identities on the users table

Adds Google Sign-In (allowlist model). A Google user is a row with a verified
``email`` (the pre-provisioned key), no ``password_hash`` and no ``username``,
and ``google_sub`` backfilled on first login. To let such rows coexist with the
existing password users, the two previously-NOT-NULL columns (``username``,
``password_hash``) are relaxed to nullable. Additive + backward compatible:
existing password rows are untouched (their columns stay populated), and Google
login is inert until ``KOEL_GOOGLE_CLIENT_ID`` is configured.

Revision ID: c9f1a3b5d7e2
Revises: b7c2e9d4a1f8
Create Date: 2026-07-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c9f1a3b5d7e2'
down_revision: str | Sequence[str] | None = 'b7c2e9d4a1f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Relax the password-era NOT NULLs so a passwordless/usernameless Google
    # identity fits the same table. Postgres unique indexes permit multiple
    # NULLs, so many Google rows (username NULL) never collide on ix_users_username.
    op.alter_column("users", "username", existing_type=sa.String(length=64), nullable=True)
    op.alter_column("users", "password_hash", existing_type=sa.LargeBinary(), nullable=True)
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("google_sub", sa.String(length=255), nullable=True))
    op.add_column(
        "users",
        sa.Column("auth_provider", sa.String(length=16), nullable=False, server_default="password"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)


def downgrade() -> None:
    # Google rows are passwordless/usernameless — restoring the NOT NULLs below
    # would fail on their NULLs. The feature is being removed, so drop those rows
    # first (while auth_provider still exists to identify them).
    op.execute("DELETE FROM users WHERE auth_provider = 'google'")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "google_sub")
    op.drop_column("users", "email")
    op.alter_column("users", "password_hash", existing_type=sa.LargeBinary(), nullable=False)
    op.alter_column("users", "username", existing_type=sa.String(length=64), nullable=False)
