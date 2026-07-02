"""Bot configuration endpoints (PRD §06/§09 Configuration Panel).

The product's moat: new use cases are configuration, not code. Richardson L2
resource endpoints over ``bot_configs`` with tenant scoping (AF) and an audit
trail (AF governance) on create/update.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from koel.common.auth import CurrentUser, TokenClaims
from koel.common.errors import ApiError
from koel.db.models import BotConfig, BotConfigAudit, UseCase
from koel.db.session import session_scope

router = APIRouter(prefix="/v1/bots", tags=["bots"])


class BotConfigIn(BaseModel):
    name: str = Field(max_length=120)
    use_case: UseCase
    language: str = Field(default="hinglish", max_length=16)
    voice_gender: str = Field(default="female", max_length=16)
    speaking_rate: float = 1.0
    greeting_template: str = Field(default="", max_length=1000)
    rag_enabled: bool = True
    rag_index: str | None = Field(default=None, max_length=64)
    rag_top_k: int | None = None
    rag_score_threshold: float | None = None
    default_tone: str = Field(default="neutral", max_length=16)
    ambient_profile: str = Field(default="callfloor", max_length=16)
    config: dict = Field(default_factory=dict)


class BotConfigPatch(BaseModel):
    """Partial in-place edit (AF). Only provided fields are changed."""

    name: str | None = Field(default=None, max_length=120)
    language: str | None = Field(default=None, max_length=16)
    voice_gender: str | None = Field(default=None, max_length=16)
    speaking_rate: float | None = None
    greeting_template: str | None = Field(default=None, max_length=1000)
    rag_enabled: bool | None = None
    rag_index: str | None = Field(default=None, max_length=64)
    rag_top_k: int | None = None
    rag_score_threshold: float | None = None
    default_tone: str | None = Field(default=None, max_length=16)
    ambient_profile: str | None = Field(default=None, max_length=16)
    config: dict | None = None
    active: bool | None = None


class BotConfigOut(BotConfigIn):
    id: str
    org_id: str | None = None
    active: bool


_AUDITED = (
    "name", "language", "voice_gender", "speaking_rate", "greeting_template",
    "rag_enabled", "rag_index", "rag_top_k", "rag_score_threshold",
    "default_tone", "ambient_profile", "config", "active",
)


def _to_out(row: BotConfig) -> BotConfigOut:
    return BotConfigOut(
        id=row.id,
        org_id=row.org_id,
        name=row.name,
        use_case=row.use_case,
        language=row.language,
        voice_gender=row.voice_gender,
        speaking_rate=row.speaking_rate,
        greeting_template=row.greeting_template,
        rag_enabled=row.rag_enabled,
        rag_index=row.rag_index,
        rag_top_k=row.rag_top_k,
        rag_score_threshold=row.rag_score_threshold,
        default_tone=row.default_tone,
        ambient_profile=row.ambient_profile,
        config=row.config or {},
        active=row.active,
    )


def _snapshot(row: BotConfig) -> dict:
    return {k: getattr(row, k) for k in _AUDITED}


@router.get("", response_model=list[BotConfigOut])
async def list_bots(user: CurrentUser) -> list[BotConfigOut]:
    stmt = select(BotConfig)
    # Tenant isolation (AF): a scoped operator sees only their org's bots.
    if user.org_id is not None:
        stmt = stmt.where(BotConfig.org_id == user.org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=BotConfigOut, status_code=status.HTTP_201_CREATED)
async def create_bot(payload: BotConfigIn, user: CurrentUser) -> BotConfigOut:
    async with session_scope() as db:
        row = BotConfig(**payload.model_dump(), org_id=user.org_id)
        db.add(row)
        await db.flush()
        db.add(BotConfigAudit(
            bot_id=row.id, user_sub=user.sub, op="create", before={}, after=_snapshot(row),
        ))
        out = _to_out(row)
    return out


@router.get("/{bot_id}", response_model=BotConfigOut)
async def get_bot(bot_id: str, user: CurrentUser) -> BotConfigOut:
    async with session_scope() as db:
        row = await _get_owned(db, bot_id, user)
    return _to_out(row)


@router.patch("/{bot_id}", response_model=BotConfigOut)
async def update_bot(bot_id: str, payload: BotConfigPatch, user: CurrentUser) -> BotConfigOut:
    changes = payload.model_dump(exclude_unset=True)
    async with session_scope() as db:
        row = await _get_owned(db, bot_id, user)
        before = _snapshot(row)
        for field, value in changes.items():
            setattr(row, field, value)
        await db.flush()
        after = _snapshot(row)
        if before != after:
            db.add(BotConfigAudit(
                bot_id=row.id, user_sub=user.sub, op="update", before=before, after=after,
            ))
        out = _to_out(row)
    return out


async def _get_owned(db, bot_id: str, user: TokenClaims) -> BotConfig:  # noqa: ANN001
    row = await db.get(BotConfig, bot_id)
    # A scoped operator cannot read/edit another org's bot — 404, not 403, so we
    # don't leak the existence of out-of-tenant resources (AF).
    if row is None or (user.org_id is not None and row.org_id != user.org_id):
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="bot_not_found",
            message=f"No bot with id {bot_id}.",
        )
    return row
