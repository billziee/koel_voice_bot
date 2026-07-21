"""user invites: token-based account provisioning + admin-triggered password reset

Adds a split invite/reset token (selector + hashed verifier, expiring) to the
users table, so a platform/org admin can provision a new operator account (or
reset an existing one's password) without ever handling their plaintext
password. The SAME mechanism serves both invite-acceptance (password_hash is
still NULL) and password reset (password_hash is already set — the row's
existing password stays valid until the token is redeemed), distinguished at
the application layer by whether password_hash is NULL when the token is
issued/accepted.

The verifier is stored hashed (sha256) — never plaintext — because a DB leak
must not hand out usable invite/reset links; the selector is a plaintext
lookup key (not secret by itself: on its own it cannot set a password without
the matching verifier), mirroring the well-known split-token pattern used for
password-reset tokens elsewhere (Laravel, Django's default token backends).

Revision ID: d8a4f6c2b9e3
Revises: c9f1a3b5d7e2
Create Date: 2026-07-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd8a4f6c2b9e3'
down_revision: str | Sequence[str] | None = 'c9f1a3b5d7e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("invite_selector", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("invite_verifier_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True))
    # Audit trail only (who provisioned/reset this account) — no FK, mirrors how
    # the rest of the schema records an actor id as a plain string (e.g.
    # EscalationRequest.operator_sub) rather than a hard foreign key.
    op.add_column("users", sa.Column("invited_by", sa.String(length=32), nullable=True))
    op.create_index("ix_users_invite_selector", "users", ["invite_selector"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_invite_selector", table_name="users")
    op.drop_column("users", "invited_by")
    op.drop_column("users", "invite_expires_at")
    op.drop_column("users", "invite_verifier_hash")
    op.drop_column("users", "invite_selector")
