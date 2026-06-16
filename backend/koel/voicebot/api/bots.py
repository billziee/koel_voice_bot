"""Bot configuration endpoints (PRD §06/§09 Configuration Panel).

The product's moat: new use cases are configuration, not code. Richardson L2
resource endpoints over ``bot_configs``.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.db.models import BotConfig, UseCase
from koel.db.session import session_scope

router = APIRouter(prefix="/v1/bots", tags=["bots"])


class BotConfigIn(BaseModel):
    name: str
    use_case: UseCase
    language: str = "hinglish"
    voice_gender: str = "female"
    speaking_rate: float = 1.0
    greeting_template: str = ""
    rag_enabled: bool = True
    rag_index: str | None = None
    default_tone: str = "neutral"
    ambient_profile: str = "callfloor"
    config: dict = Field(default_factory=dict)


class BotConfigOut(BotConfigIn):
    id: str
    active: bool


def _to_out(row: BotConfig) -> BotConfigOut:
    return BotConfigOut(
        id=row.id,
        name=row.name,
        use_case=row.use_case,
        language=row.language,
        voice_gender=row.voice_gender,
        speaking_rate=row.speaking_rate,
        greeting_template=row.greeting_template,
        rag_enabled=row.rag_enabled,
        rag_index=row.rag_index,
        default_tone=row.default_tone,
        ambient_profile=row.ambient_profile,
        config=row.config or {},
        active=row.active,
    )


@router.get("", response_model=list[BotConfigOut])
async def list_bots(_: CurrentUser) -> list[BotConfigOut]:
    async with session_scope() as db:
        rows = (await db.execute(select(BotConfig))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=BotConfigOut, status_code=status.HTTP_201_CREATED)
async def create_bot(payload: BotConfigIn, _: CurrentUser) -> BotConfigOut:
    async with session_scope() as db:
        row = BotConfig(**payload.model_dump())
        db.add(row)
        await db.flush()
        out = _to_out(row)
    return out


@router.get("/{bot_id}", response_model=BotConfigOut)
async def get_bot(bot_id: str, _: CurrentUser) -> BotConfigOut:
    async with session_scope() as db:
        row = await db.get(BotConfig, bot_id)
    if row is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="bot_not_found",
            message=f"No bot with id {bot_id}.",
        )
    return _to_out(row)
