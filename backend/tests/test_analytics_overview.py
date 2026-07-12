"""Advanced analytics overview: funnel + objections + sentiment (6.2)."""

from __future__ import annotations

from koel.common.auth import issue_token
from koel.common.schemas import Language
from koel.voicebot.persistence import persist_call, persist_call_summary
from koel.voicebot.session.models import SessionState, Turn


def _call(sid: str, outcome: str, org: str | None = None) -> SessionState:
    s = SessionState(session_id=sid, bot_id="b", customer_id="c", use_case="collections")
    s.org_id = org
    s.language = Language.hinglish
    s.outcome = outcome
    s.add_turn(Turn(index=0, speaker="bot", text="Namaste"))
    return s


def _hdr(org: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', role='operator', org_id=org)}"}


async def test_overview_reports_funnel_objections_and_sentiment(app_client, test_db):
    client, _ = app_client
    await persist_call(_call("c1", "converted"))
    await persist_call(_call("c2", "escalated"))
    await persist_call(_call("c3", "declined"))
    await persist_call_summary("c2", {"objection": "already have a card", "sentiment": "negative"})
    await persist_call_summary("c3", {"objection": "already have a card", "sentiment": "neutral"})

    r = await client.get("/v1/analytics/overview", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 3
    assert body["funnel"]["converted"] == 1
    assert body["funnel"]["escalated"] == 1
    assert body["funnel"]["declined"] == 1
    assert abs(body["conversion_rate"] - 0.3333) < 0.01
    # The repeated objection ranks first, counted twice.
    assert body["top_objections"][0] == {"objection": "already have a card", "count": 2}
    assert body["sentiment"].get("negative") == 1


async def test_overview_is_tenant_scoped(app_client, test_db):
    client, _ = app_client
    await persist_call(_call("o1", "converted", org="orgA"))
    await persist_call(_call("o2", "converted", org="orgB"))
    r = await client.get("/v1/analytics/overview", headers=_hdr(org="orgA"))
    assert r.json()["total_calls"] == 1


async def test_overview_requires_auth(app_client, test_db):
    client, _ = app_client
    assert (await client.get("/v1/analytics/overview")).status_code == 401
