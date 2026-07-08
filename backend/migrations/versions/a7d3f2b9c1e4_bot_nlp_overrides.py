"""bot nlp overrides (5.4): llm_model, llm_temperature, prompt_preamble

Additive, nullable columns on bot_configs so existing rows fall back to the
NLP_svc service defaults (NULL). No backfill needed.

Revision ID: a7d3f2b9c1e4
Revises: f1e8bee45d41
Create Date: 2026-07-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a7d3f2b9c1e4'
down_revision: str | Sequence[str] | None = 'f1e8bee45d41'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('bot_configs', sa.Column('llm_model', sa.String(length=64), nullable=True))
    op.add_column('bot_configs', sa.Column('llm_temperature', sa.Float(), nullable=True))
    op.add_column('bot_configs', sa.Column('prompt_preamble', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('bot_configs', 'prompt_preamble')
    op.drop_column('bot_configs', 'llm_temperature')
    op.drop_column('bot_configs', 'llm_model')
