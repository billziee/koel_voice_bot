"""Security-header + CORS fail-fast tests (PRD §08 / web security)."""

from __future__ import annotations

import httpx
import pytest

from koel.common.app_factory import create_app
from koel.common.config import CommonSettings


async def test_security_headers_present_on_api_responses():
    app = create_app(service_name="hdr_test", title="hdr")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in r.headers["permissions-policy"]
    assert "default-src 'self'" in r.headers["content-security-policy"]


async def test_csp_skipped_on_docs_so_swagger_still_loads():
    app = create_app(service_name="hdr_test2", title="hdr2")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/openapi.json")
    assert "content-security-policy" not in {k.lower() for k in r.headers}
    assert r.headers["x-content-type-options"] == "nosniff"  # non-CSP headers still apply


def test_production_rejects_wildcard_and_empty_cors():
    for cors in ("*", ""):
        s = CommonSettings(
            environment="production", jwt_secret="a-strong-non-default-secret", cors_allow_origins=cors
        )
        with pytest.raises(RuntimeError):
            create_app(service_name="prod_cors", title="p", settings=s)


def test_production_rejects_default_jwt_secret():
    s = CommonSettings(environment="production", cors_allow_origins="https://app.example.com")
    with pytest.raises(RuntimeError):
        create_app(service_name="prod_jwt", title="p", settings=s)
