"""Live supervisor monitor (3.3): registry + operator read API."""

from __future__ import annotations

from koel.common.auth import issue_token
from koel.voicebot.monitor import LiveMonitor


def test_monitor_open_record_status_get_close():
    m = LiveMonitor()
    m.open("s1", "bot_x", "org1")
    assert [c["session_id"] for c in m.list_active(org_id="org1")] == ["s1"]

    m.record_turn("s1", "bot", "Namaste")
    m.record_turn("s1", "customer", "Haan")
    m.set_status("s1", "processing")
    detail = m.get("s1", "org1")
    assert detail["turn_count"] == 2
    assert detail["status"] == "processing"
    assert [e["speaker"] for e in detail["events"]] == ["bot", "customer"]

    m.close("s1")
    assert m.list_active(org_id="org1") == []
    assert m.get("s1", "org1") is None


def test_monitor_is_tenant_scoped():
    m = LiveMonitor()
    m.open("a", "bot", "orgA")
    m.open("b", "bot", "orgB")
    assert [c["session_id"] for c in m.list_active("orgA")] == ["a"]
    assert m.get("a", "orgB") is None  # cross-tenant → hidden
    # An unscoped/admin caller sees everything.
    assert len(m.list_active(None)) == 2


def test_monitor_events_are_capped():
    m = LiveMonitor()
    m.open("s", "bot", None)
    for i in range(120):
        m.record_turn("s", "bot", f"line {i}")
    detail = m.get("s", None)
    assert detail["turn_count"] == 120  # count is cumulative
    assert len(detail["events"]) == 50  # buffer is bounded
    assert detail["events"][-1]["text"] == "line 119"  # keeps the most recent


def _hdr(org: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('sup', kind='access', role='operator', org_id=org)}"}


async def test_monitor_api_lists_and_details_live_calls(app_client, test_db):
    client, runtime = app_client
    runtime.monitor.open("live9", "bot_x", None)
    runtime.monitor.record_turn("live9", "bot", "Namaste ji")
    runtime.monitor.set_status("live9", "listening")

    r = await client.get("/v1/operator/monitor", headers=_hdr())
    assert r.status_code == 200
    assert any(c["session_id"] == "live9" and c["status"] == "listening" for c in r.json())

    r = await client.get("/v1/operator/monitor/live9", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["events"][0]["text"] == "Namaste ji"

    # Unknown / ended call → 404.
    assert (await client.get("/v1/operator/monitor/nope", headers=_hdr())).status_code == 404


async def test_monitor_api_requires_auth(app_client, test_db):
    client, _ = app_client
    assert (await client.get("/v1/operator/monitor")).status_code == 401
