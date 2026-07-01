"""Cross-tenant IDOR regressions (audit: calls, sessions, stats org scoping)."""

from __future__ import annotations

from datetime import UTC, datetime

from koel.common.auth import issue_token
from koel.db.models import CallLog, CallOutcome
from tests.conftest import seed_bot


def _hdr(org_id: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


async def _seed_call(maker, call_id, org_id, bot_id="bot_globex"):
    async with maker() as s:
        s.add(CallLog(
            id=call_id, session_id=call_id, bot_id=bot_id, org_id=org_id, language="hinglish",
            outcome=CallOutcome.converted, started_at=datetime.now(UTC), duration_ms=1000,
        ))
        await s.commit()


async def test_get_call_blocks_cross_tenant_transcript(app_client, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_globex", org_id="globex")
    await _seed_call(test_db, "call_g", "globex")
    # Acme operator must NOT read globex's call transcript.
    assert (await client.get("/v1/calls/call_g", headers=_hdr("acme"))).status_code == 404
    # The owning org can read it.
    assert (await client.get("/v1/calls/call_g", headers=_hdr("globex"))).status_code == 200
    # An unscoped (single-tenant) operator still can.
    assert (await client.get("/v1/calls/call_g", headers=_hdr(None))).status_code == 200


async def test_start_session_blocks_cross_tenant_bot(app_client, test_db):
    client, runtime = app_client
    from koel.crm.schemas import CustomerRecord
    from koel.db.models import UseCase
    runtime.clients.customer = CustomerRecord(
        id="cust_x", name="R", phone="+91", language="hinglish", bank="HDFC",
        use_case=UseCase.credit_card,
    )
    await seed_bot(test_db, id="bot_globex", org_id="globex")
    # Acme cannot spin a session on globex's bot (would hijack its config).
    r = await client.post("/v1/sessions", headers=_hdr("acme"),
                          json={"bot_id": "bot_globex", "customer_id": "cust_x"})
    assert r.status_code == 404


async def test_bot_stats_blocks_cross_tenant(app_client, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_globex", org_id="globex")
    await _seed_call(test_db, "c1", "globex")
    # Acme must not read globex bot stats (existence not leaked → 404).
    assert (await client.get("/v1/bots/bot_globex/stats", headers=_hdr("acme"))).status_code == 404
    # Owner sees the real aggregate.
    r = await client.get("/v1/bots/bot_globex/stats", headers=_hdr("globex"))
    assert r.status_code == 200 and r.json()["total_calls"] == 1
