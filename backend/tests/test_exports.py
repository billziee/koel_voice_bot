"""Regulatory audit-export bundles (6.5)."""

from __future__ import annotations

import hashlib
import json

from koel.common.auth import issue_token
from koel.common.schemas import Language, Tone
from koel.voicebot.exports import build_export_bundle
from koel.voicebot.persistence import persist_call
from koel.voicebot.session.models import SessionState, Turn


def _call(session_id: str, org_id: str | None, outcome: str = "converted") -> SessionState:
    s = SessionState(session_id=session_id, bot_id="bot_test", customer_id="c1", use_case="credit_card")
    s.org_id = org_id
    s.language = Language.hinglish
    s.outcome = outcome
    s.consent_captured = True
    s.add_turn(Turn(index=0, speaker="bot", text="Namaste", tone=Tone.cheerful, intent="greeting"))
    s.add_turn(Turn(index=1, speaker="customer", text="Haan boliye", confidence=0.9))
    return s


async def test_bundle_is_hashed_deterministic_and_counted(test_db):
    await persist_call(_call("e1", "org1"))
    await persist_call(_call("e2", "org1"))

    payload, count, sha = await build_export_bundle(org_id="org1", start=None, end=None)
    assert count == 2
    assert sha == hashlib.sha256(payload).hexdigest()

    lines = payload.decode().strip().split("\n")
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["session_id"] in ("e1", "e2")
    assert rec["transcript"] and rec["metrics"]["consent_captured"] is True

    # Deterministic (sorted keys + stable order) → same manifest on re-export.
    _, _, sha2 = await build_export_bundle(org_id="org1", start=None, end=None)
    assert sha2 == sha


async def test_bundle_is_tenant_scoped(test_db):
    await persist_call(_call("a1", "orgA"))
    await persist_call(_call("b1", "orgB"))
    _, count, _ = await build_export_bundle(org_id="orgA", start=None, end=None)
    assert count == 1


def _hdr(org: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', role='operator', org_id=org)}"}


async def test_export_api_returns_inline_without_a_bucket(app_client, test_db):
    client, _ = app_client
    await persist_call(_call("live_e", None))

    r = await client.post("/v1/exports", headers=_hdr(), json={})
    assert r.status_code == 201
    body = r.json()
    assert body["record_count"] >= 1
    assert body["object_url"] is None  # no bucket configured → inline delivery
    assert body["bundle"] and "live_e" in body["bundle"]
    assert len(body["manifest_sha256"]) == 64

    # The export is itself recorded in the audit trail.
    listing = await client.get("/v1/exports", headers=_hdr())
    assert listing.status_code == 200
    assert any(j["id"] == body["job_id"] for j in listing.json())


async def test_export_requires_auth(app_client, test_db):
    client, _ = app_client
    assert (await client.post("/v1/exports", json={})).status_code == 401
