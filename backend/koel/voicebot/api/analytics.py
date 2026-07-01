"""Call analytics aggregation (AE, PRD §09 dashboard).

Replaces the frontend's hardcoded/random dashboard numbers with real aggregates
over ``call_logs``. Tenant-scoped (AF): a scoped operator only sees their org.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from koel.common.auth import CurrentUser
from koel.db.models import CallLog, CallOutcome
from koel.db.session import session_scope

router = APIRouter(prefix="/v1", tags=["analytics"])


class BotStats(BaseModel):
    bot_id: str
    total_calls: int
    converted: int
    escalated: int
    conversion_rate: float  # converted / total, 0..1
    escalation_rate: float  # escalated / total, 0..1
    avg_duration_ms: int
    outcomes: dict[str, int]  # outcome value → count


def _rate(numerator: int, total: int) -> float:
    return round(numerator / total, 4) if total else 0.0


async def _aggregate(bot_id: str | None, org_id: str | None) -> list[BotStats]:
    """One aggregation pass grouped by bot (optionally filtered to one bot)."""
    scope = []
    if bot_id:
        scope.append(CallLog.bot_id == bot_id)
    if org_id is not None:
        scope.append(CallLog.org_id == org_id)

    # Per-(bot, outcome) counts + duration sums in a single grouped query — no
    # N+1, no full-table load (reuses the history.py query posture).
    stmt = select(
        CallLog.bot_id,
        CallLog.outcome,
        func.count().label("n"),
        func.coalesce(func.sum(CallLog.duration_ms), 0).label("dur"),
    ).group_by(CallLog.bot_id, CallLog.outcome)
    for cond in scope:
        stmt = stmt.where(cond)

    async with session_scope() as db:
        rows = (await db.execute(stmt)).all()

    agg: dict[str, dict] = {}
    for row_bot_id, outcome, n, dur in rows:
        b = agg.setdefault(
            row_bot_id, {"total": 0, "converted": 0, "escalated": 0, "dur": 0, "outcomes": {}}
        )
        b["total"] += n
        b["dur"] += int(dur or 0)
        b["outcomes"][outcome.value] = b["outcomes"].get(outcome.value, 0) + n
        if outcome is CallOutcome.converted:
            b["converted"] += n
        if outcome is CallOutcome.escalated:
            b["escalated"] += n

    return [
        BotStats(
            bot_id=bid,
            total_calls=b["total"],
            converted=b["converted"],
            escalated=b["escalated"],
            conversion_rate=_rate(b["converted"], b["total"]),
            escalation_rate=_rate(b["escalated"], b["total"]),
            avg_duration_ms=int(b["dur"] / b["total"]) if b["total"] else 0,
            outcomes=b["outcomes"],
        )
        for bid, b in agg.items()
    ]


@router.get("/bots/{bot_id}/stats", response_model=BotStats)
async def bot_stats(bot_id: str, user: CurrentUser) -> BotStats:
    stats = await _aggregate(bot_id, user.org_id)
    if stats:
        return stats[0]
    # No calls yet → a well-formed zeroed summary (not a 404).
    return BotStats(
        bot_id=bot_id,
        total_calls=0,
        converted=0,
        escalated=0,
        conversion_rate=0.0,
        escalation_rate=0.0,
        avg_duration_ms=0,
        outcomes={},
    )


@router.get("/stats", response_model=list[BotStats])
async def all_bot_stats(user: CurrentUser) -> list[BotStats]:
    return await _aggregate(None, user.org_id)
