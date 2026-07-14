"""Cross-tenant leak gate (6.4 Multi-tenant enablement).

THE isolation gate. Org scoping is pervasive across the schema/routers, but this
suite is what PROVES org A can never read (or act on) org B's data. For each
org-scoped router reachable through the ``app_client`` fixture we seed rows under
two tenants ("org_a" / "org_b") and assert:

  * a token scoped to org A sees ONLY A's rows (B's are absent / 404),
  * a token scoped to org B sees ONLY B's rows,
  * an unscoped (platform-admin / single-tenant) token sees everything.

Routers covered: bots (``/v1/bots``), call history (``/v1/calls``), analytics
(``/v1/stats`` + ``/v1/analytics/overview``), exports (``/v1/exports``), the
operator handoff queue (``/v1/operator/queue``), and the session-start guard
(``/v1/sessions``, the original IDOR regression).

NOT covered: the knowledge-base router (``/v1/knowledge-*``). ``KnowledgeChunk``
uses the pgvector ``Vector`` column type, so it is deliberately excluded from the
SQLite test tables (``conftest._SQLITE_TABLES``) and create requires a live
embedder — it can't be exercised through this in-memory fixture. Its org filter
is identical in shape to the routers proven here (``.where(org_id == ...)`` with
an ``org_id is None`` bypass) and runs against Postgres in the CI service run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from koel.common.auth import issue_token
from koel.db.models import (
    CallLog,
    CallOutcome,
    EscalationRequest,
    EscalationStatus,
    ExportJob,
)
from tests.conftest import seed_bot

ORG_A = "org_a"
ORG_B = "org_b"


def _hdr(org_id: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


async def _seed_call(maker, call_id, org_id, bot_id="bot_globex"):  # noqa: ANN001
    async with maker() as s:
        s.add(CallLog(
            id=call_id, session_id=call_id, bot_id=bot_id, org_id=org_id, language="hinglish",
            outcome=CallOutcome.converted, started_at=datetime.now(UTC), duration_ms=1000,
        ))
        await s.commit()


async def _seed_export(maker, *, job_id: str, org_id: str | None) -> None:
    async with maker() as s:
        s.add(ExportJob(
            id=job_id, org_id=org_id, requested_by="op", record_count=1, manifest_sha256="0" * 64,
        ))
        await s.commit()


async def _seed_escalation(maker, *, esc_id: str, org_id: str | None, bot_id: str) -> None:
    async with maker() as s:
        s.add(EscalationRequest(
            id=esc_id, org_id=org_id, session_id=f"sess_{esc_id}", bot_id=bot_id,
            status=EscalationStatus.pending,
        ))
        await s.commit()


# --- Bot configs (/v1/bots) -------------------------------------------------


async def test_bots_list_and_get_are_tenant_isolated(app_client, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_a", name="A Bot", org_id=ORG_A)
    await seed_bot(test_db, id="bot_b", name="B Bot", org_id=ORG_B)

    a_ids = {b["id"] for b in (await client.get("/v1/bots", headers=_hdr(ORG_A))).json()}
    b_ids = {b["id"] for b in (await client.get("/v1/bots", headers=_hdr(ORG_B))).json()}
    assert a_ids == {"bot_a"}
    assert b_ids == {"bot_b"}

    # Cross-tenant fetch is 404 (existence not leaked); own fetch is 200.
    assert (await client.get("/v1/bots/bot_b", headers=_hdr(ORG_A))).status_code == 404
    assert (await client.get("/v1/bots/bot_a", headers=_hdr(ORG_A))).status_code == 200

    all_ids = {b["id"] for b in (await client.get("/v1/bots", headers=_hdr(None))).json()}
    assert {"bot_a", "bot_b"} <= all_ids


# --- Call history (/v1/calls) — original IDOR regression + list scoping -----


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


async def test_call_history_list_is_tenant_isolated(app_client, test_db):
    client, _ = app_client
    await _seed_call(test_db, "call_a", ORG_A, bot_id="bot_a")
    await _seed_call(test_db, "call_b", ORG_B, bot_id="bot_b")

    a_ids = {c["id"] for c in (await client.get("/v1/calls", headers=_hdr(ORG_A))).json()}
    b_ids = {c["id"] for c in (await client.get("/v1/calls", headers=_hdr(ORG_B))).json()}
    assert a_ids == {"call_a"}
    assert b_ids == {"call_b"}

    all_ids = {c["id"] for c in (await client.get("/v1/calls", headers=_hdr(None))).json()}
    assert {"call_a", "call_b"} <= all_ids


# --- Session start guard (/v1/sessions) — original IDOR regression ----------


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


# --- Analytics (/v1/stats + /v1/bots/{id}/stats + /v1/analytics/overview) ---


async def test_bot_stats_blocks_cross_tenant(app_client, test_db):
    client, _ = app_client
    await seed_bot(test_db, id="bot_globex", org_id="globex")
    await _seed_call(test_db, "c1", "globex")
    # Acme must not read globex bot stats (existence not leaked → 404).
    assert (await client.get("/v1/bots/bot_globex/stats", headers=_hdr("acme"))).status_code == 404
    # Owner sees the real aggregate.
    r = await client.get("/v1/bots/bot_globex/stats", headers=_hdr("globex"))
    assert r.status_code == 200 and r.json()["total_calls"] == 1


async def test_analytics_list_and_overview_are_tenant_isolated(app_client, test_db):
    client, _ = app_client
    await _seed_call(test_db, "call_a1", ORG_A, bot_id="bot_a")
    await _seed_call(test_db, "call_a2", ORG_A, bot_id="bot_a")
    await _seed_call(test_db, "call_b1", ORG_B, bot_id="bot_b")

    # /v1/stats groups by bot — each tenant sees only its own bot's row.
    a_bots = {s["bot_id"] for s in (await client.get("/v1/stats", headers=_hdr(ORG_A))).json()}
    b_bots = {s["bot_id"] for s in (await client.get("/v1/stats", headers=_hdr(ORG_B))).json()}
    assert a_bots == {"bot_a"}
    assert b_bots == {"bot_b"}

    # /v1/analytics/overview totals are org-scoped; unscoped sees the union.
    a_total = (await client.get("/v1/analytics/overview", headers=_hdr(ORG_A))).json()["total_calls"]
    b_total = (await client.get("/v1/analytics/overview", headers=_hdr(ORG_B))).json()["total_calls"]
    all_total = (await client.get("/v1/analytics/overview", headers=_hdr(None))).json()["total_calls"]
    assert a_total == 2
    assert b_total == 1
    assert all_total == 3


# --- Regulatory exports (/v1/exports) ---------------------------------------


async def test_exports_list_is_tenant_isolated(app_client, test_db):
    client, _ = app_client
    await _seed_export(test_db, job_id="exp_a", org_id=ORG_A)
    await _seed_export(test_db, job_id="exp_b", org_id=ORG_B)

    a_ids = {j["id"] for j in (await client.get("/v1/exports", headers=_hdr(ORG_A))).json()}
    b_ids = {j["id"] for j in (await client.get("/v1/exports", headers=_hdr(ORG_B))).json()}
    assert a_ids == {"exp_a"}
    assert b_ids == {"exp_b"}

    all_ids = {j["id"] for j in (await client.get("/v1/exports", headers=_hdr(None))).json()}
    assert {"exp_a", "exp_b"} <= all_ids


# --- Operator handoff queue (/v1/operator/queue) ----------------------------


async def test_operator_queue_is_tenant_isolated(app_client, test_db):
    client, _ = app_client
    await _seed_escalation(test_db, esc_id="esc_a", org_id=ORG_A, bot_id="bot_a")
    await _seed_escalation(test_db, esc_id="esc_b", org_id=ORG_B, bot_id="bot_b")

    a_ids = {e["id"] for e in (await client.get("/v1/operator/queue", headers=_hdr(ORG_A))).json()}
    b_ids = {e["id"] for e in (await client.get("/v1/operator/queue", headers=_hdr(ORG_B))).json()}
    assert a_ids == {"esc_a"}
    assert b_ids == {"esc_b"}

    all_ids = {e["id"] for e in (await client.get("/v1/operator/queue", headers=_hdr(None))).json()}
    assert {"esc_a", "esc_b"} <= all_ids
