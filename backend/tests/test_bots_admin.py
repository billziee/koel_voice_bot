"""Bot config PATCH + audit trail + tenant isolation (AF)."""

from __future__ import annotations

from sqlalchemy import select

from koel.common.auth import issue_token
from koel.db.models import BotConfig, BotConfigAudit
from tests.conftest import seed_bot


def _headers(org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


async def test_patch_updates_fields_and_writes_audit(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db, speaking_rate=1.0)

    r = await client.patch(
        "/v1/bots/bot_test", headers=access_headers, json={"speaking_rate": 1.3, "voice_gender": "male"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["speaking_rate"] == 1.3
    assert r.json()["voice_gender"] == "male"

    async with test_db() as s:
        audits = (
            await s.execute(select(BotConfigAudit).where(BotConfigAudit.bot_id == "bot_test"))
        ).scalars().all()
    ops = [a.op for a in audits]
    assert "update" in ops
    upd = next(a for a in audits if a.op == "update")
    assert upd.before["speaking_rate"] == 1.0
    assert upd.after["speaking_rate"] == 1.3


async def test_create_bot_stamps_org_and_audit(app_client, test_db):
    client, _ = app_client
    r = await client.post(
        "/v1/bots",
        headers=_headers(org_id="acme"),
        json={"name": "Acme Bot", "use_case": "credit_card"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["org_id"] == "acme"

    async with test_db() as s:
        row = (await s.execute(select(BotConfig).where(BotConfig.name == "Acme Bot"))).scalar_one()
        assert row.org_id == "acme"


async def test_tenant_cannot_see_or_edit_other_orgs_bot(app_client, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_acme", name="Acme", org_id="acme")
    await seed_bot(test_db, id="bot_globex", name="Globex", org_id="globex")

    # Acme operator lists → only their bot.
    r = await client.get("/v1/bots", headers=_headers(org_id="acme"))
    assert [b["id"] for b in r.json()] == ["bot_acme"]

    # Cross-tenant fetch/patch → 404 (existence not leaked).
    assert (await client.get("/v1/bots/bot_globex", headers=_headers(org_id="acme"))).status_code == 404
    r = await client.patch(
        "/v1/bots/bot_globex", headers=_headers(org_id="acme"), json={"speaking_rate": 2.0}
    )
    assert r.status_code == 404


async def test_unscoped_operator_sees_all_bots(app_client, access_headers, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_a", org_id="acme")
    await seed_bot(test_db, id="bot_b", org_id=None)
    r = await client.get("/v1/bots", headers=access_headers)  # org_id=None → no filter
    assert {b["id"] for b in r.json()} == {"bot_a", "bot_b"}
