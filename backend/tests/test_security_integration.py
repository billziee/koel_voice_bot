"""Security integration checks (R): rate-limit rejection + telemetry smoke."""

from __future__ import annotations

from koel.common.telemetry import (
    record_db_pool_held,
    record_db_pool_in_use,
    record_error,
    record_hop_latency,
    record_nlp_usage,
    record_turn_latency,
)


async def test_login_rate_limit_returns_429(app_client, test_db):
    client, _ = app_client
    # The login limiter caps at 8/min per IP; the 9th must be rejected with 429.
    codes = []
    for _ in range(10):
        r = await client.post("/v1/auth/login", json={"username": "demo", "password": "nope"})
        codes.append(r.status_code)
    assert 429 in codes
    assert codes.count(401) <= 8  # only the allowed attempts reached the handler


async def test_session_creation_requires_auth(app_client, test_db):
    client, _ = app_client
    r = await client.post("/v1/sessions", json={"bot_id": "bot_test"})
    assert r.status_code == 401  # missing bearer token


def test_metric_helpers_are_safe_before_configure():
    # Recording before configure_metrics (unit tests) must be a no-op, never raise.
    record_turn_latency(123.4)
    record_hop_latency("nlp", 50.0)
    record_error("tts")
    record_nlp_usage(1000, 0.0007)
    record_db_pool_in_use(3)  # O-10 pool observability helpers, same no-op guard
    record_db_pool_held(12.5)
