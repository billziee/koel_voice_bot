"""Login → tenant-scoping, end-to-end (6.4 follow-up).

The isolation gate in ``test_tenant_isolation`` mints tokens directly via
``issue_token``. THIS suite proves the same isolation holds when the token is
obtained the real way — through ``POST /v1/auth/login`` — closing the loop the
follow-up was about: a seeded org-A operator logs in, and the token they receive
scopes every subsequent read to org A. The platform-admin (``demo``) login sees
every tenant.

Users + orgs are seeded into the in-memory DB by the ``test_db`` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime

from koel.db.models import CallLog, CallOutcome
from koel.voicebot.api import auth as auth_mod
from tests.conftest import seed_bot

ORG_A = "orgA"
ORG_B = "orgB"


def _disable_login_limit() -> None:
    from koel.voicebot.main import app

    app.dependency_overrides[auth_mod._login_limit] = lambda: None


async def _login(client, username: str, password: str) -> dict[str, str]:
    r = await client.post("/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_call(maker, call_id: str, org_id: str, bot_id: str) -> None:
    async with maker() as s:
        s.add(
            CallLog(
                id=call_id, session_id=call_id, bot_id=bot_id, org_id=org_id,
                language="hinglish", outcome=CallOutcome.converted,
                started_at=datetime.now(UTC), duration_ms=1000,
            )
        )
        await s.commit()


async def test_login_scopes_calls_to_operator_org(app_client, test_db):
    client, _ = app_client
    _disable_login_limit()
    await _seed_call(test_db, "call_a", ORG_A, bot_id="bot_a")
    await _seed_call(test_db, "call_b", ORG_B, bot_id="bot_b")

    # Real login as the org-A operator → token scoped to org A.
    a_headers = await _login(client, "orga_operator", "orga-demo")
    a_ids = {c["id"] for c in (await client.get("/v1/calls", headers=a_headers)).json()}
    assert a_ids == {"call_a"}  # org B's call is invisible

    # Cross-tenant fetch of org B's call is 404 (existence not leaked).
    assert (await client.get("/v1/calls/call_b", headers=a_headers)).status_code == 404
    assert (await client.get("/v1/calls/call_a", headers=a_headers)).status_code == 200


async def test_login_scopes_bots_to_operator_org(app_client, test_db):
    client, _ = app_client
    _disable_login_limit()
    await seed_bot(test_db, id="bot_a", name="A Bot", org_id=ORG_A)
    await seed_bot(test_db, id="bot_b", name="B Bot", org_id=ORG_B)

    b_headers = await _login(client, "orgb_operator", "orgb-demo")
    b_ids = {b["id"] for b in (await client.get("/v1/bots", headers=b_headers)).json()}
    assert b_ids == {"bot_b"}
    assert (await client.get("/v1/bots/bot_a", headers=b_headers)).status_code == 404


async def test_platform_admin_login_sees_all_tenants(app_client, test_db):
    client, _ = app_client
    _disable_login_limit()
    await _seed_call(test_db, "call_a", ORG_A, bot_id="bot_a")
    await _seed_call(test_db, "call_b", ORG_B, bot_id="bot_b")

    # The demo platform-admin (org_id=None) login sees BOTH orgs' calls.
    admin_headers = await _login(client, "demo", "koel-demo")
    all_ids = {c["id"] for c in (await client.get("/v1/calls", headers=admin_headers)).json()}
    assert {"call_a", "call_b"} <= all_ids
    assert (await client.get("/v1/calls/call_a", headers=admin_headers)).status_code == 200
    assert (await client.get("/v1/calls/call_b", headers=admin_headers)).status_code == 200
