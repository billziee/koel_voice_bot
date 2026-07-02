"""Call analytics aggregation (AE) + tenant scoping."""

from __future__ import annotations

from koel.common.auth import issue_token
from koel.db.models import CallLog, CallOutcome
from tests.conftest import seed_bot


def _headers(org_id=None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


async def _seed_calls(maker) -> None:
    rows = [
        ("bot_test", CallOutcome.converted, 30_000, "acme"),
        ("bot_test", CallOutcome.converted, 20_000, "acme"),
        ("bot_test", CallOutcome.declined, 10_000, "acme"),
        ("bot_test", CallOutcome.escalated, 40_000, "acme"),
        ("bot_other", CallOutcome.converted, 5_000, "globex"),
    ]
    async with maker() as s:
        for bot_id, outcome, dur, org in rows:
            s.add(CallLog(
                session_id=f"s_{bot_id}_{outcome.value}_{dur}",
                bot_id=bot_id, org_id=org, language="hinglish",
                outcome=outcome, duration_ms=dur,
                escalated=(outcome is CallOutcome.escalated),
            ))
        await s.commit()


async def test_bot_stats_aggregates_conversion_and_duration(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db)
    await _seed_calls(test_db)

    r = await client.get("/v1/bots/bot_test/stats", headers=access_headers)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["total_calls"] == 4
    assert s["converted"] == 2
    assert s["escalated"] == 1
    assert s["conversion_rate"] == 0.5
    assert s["escalation_rate"] == 0.25
    assert s["avg_duration_ms"] == 25_000  # (30+20+10+40)/4 k
    assert s["outcomes"]["converted"] == 2


async def test_stats_zeroed_when_no_calls(app_client, access_headers, test_db):
    client, _ = app_client
    r = await client.get("/v1/bots/ghost/stats", headers=access_headers)
    assert r.status_code == 200
    assert r.json()["total_calls"] == 0
    assert r.json()["conversion_rate"] == 0.0


async def test_stats_are_tenant_scoped(app_client, test_db):
    client, _ = app_client
    await _seed_calls(test_db)
    # globex operator only sees the one globex call, not acme's.
    r = await client.get("/v1/stats", headers=_headers(org_id="globex"))
    ids = {row["bot_id"]: row for row in r.json()}
    assert set(ids) == {"bot_other"}
    assert ids["bot_other"]["total_calls"] == 1
