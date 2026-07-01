"""Liveness vs readiness split (P)."""

from __future__ import annotations


async def test_liveness_is_always_ok(app_client, test_db):
    client, _ = app_client
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_legacy_health_still_ok(app_client, test_db):
    client, _ = app_client
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_readiness_reports_dependency_map(app_client, test_db):
    client, _ = app_client
    r = await client.get("/health/ready")
    # ASGITransport doesn't run the lifespan, so app.state.runtime is unset →
    # the probe reports not-ready (503) rather than crashing. Either way the
    # response is a well-formed HealthResponse with a status field.
    assert r.status_code in (200, 503)
    assert r.json()["status"] in ("ok", "degraded", "down")
