"""Call analytics aggregation (AE, PRD §09 dashboard).

Replaces the frontend's hardcoded/random dashboard numbers with real aggregates
over ``call_logs``. Tenant-scoped (AF): a scoped operator only sees their org.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from koel.common.auth import CurrentUser
from koel.common.errors import ApiError
from koel.db.models import BotConfig, CallLog, CallOutcome
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
    # Tenant isolation (AF): a scoped operator can only read their own bots'
    # stats — 404 for anything else so a bot's existence in another org isn't leaked.
    if user.org_id is not None:
        async with session_scope() as db:
            bot = await db.get(BotConfig, bot_id)
        if bot is None or bot.org_id != user.org_id:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="bot_not_found",
                message=f"No bot with id {bot_id}.",
            )
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


# --- Overview: funnel + objections + sentiment (6.2) ------------------------


class ObjectionCount(BaseModel):
    objection: str
    count: int


class AnalyticsOverview(BaseModel):
    total_calls: int
    funnel: dict[str, int]  # outcome value → count (the campaign funnel stages)
    conversion_rate: float  # 0..1
    escalation_rate: float  # 0..1
    top_objections: list[ObjectionCount] = Field(default_factory=list)
    sentiment: dict[str, int] = Field(default_factory=dict)  # sentiment tag → count


@router.get("/analytics/overview", response_model=AnalyticsOverview)
async def analytics_overview(user: CurrentUser) -> AnalyticsOverview:
    """Cross-bot funnel plus the top objections and sentiment breakdown, mined
    from the post-call summaries persisted on ``CallLog.metrics`` (5.3). Tenant-
    scoped (AF). Aggregated in Python over an org-scoped fetch so it's portable
    across the JSON-column dialects (SQLite in tests, Postgres in prod) —
    fine at MVP volume; a SQL-native rollup is the scale-up path (O-10)."""
    stmt = select(CallLog.outcome, CallLog.metrics)
    if user.org_id is not None:
        stmt = stmt.where(CallLog.org_id == user.org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).all()

    funnel: dict[str, int] = {}
    objections: Counter[str] = Counter()
    sentiment: Counter[str] = Counter()
    converted = escalated = 0
    for outcome, metrics in rows:
        funnel[outcome.value] = funnel.get(outcome.value, 0) + 1
        if outcome is CallOutcome.converted:
            converted += 1
        if outcome is CallOutcome.escalated:
            escalated += 1
        summary = (metrics or {}).get("summary") or {}
        objection = str(summary.get("objection") or "").strip()
        if objection:
            objections[objection] += 1
        sent = str(summary.get("sentiment") or "").strip()
        if sent:
            sentiment[sent] += 1

    total = len(rows)
    return AnalyticsOverview(
        total_calls=total,
        funnel=funnel,
        conversion_rate=_rate(converted, total),
        escalation_rate=_rate(escalated, total),
        top_objections=[ObjectionCount(objection=o, count=c) for o, c in objections.most_common(10)],
        sentiment=dict(sentiment),
    )
