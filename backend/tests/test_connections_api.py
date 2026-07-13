"""BYOK model-connection CRUD API (koel/voicebot/api/connections.py, Phase 2b).

Covers the org-scoped CRUD happy path, the key never appearing in any response,
cross-tenant isolation, the secrets-disabled + validation error mappings, and the
``/test`` connectivity probe for each kind. The ``/test`` LLM probe monkeypatches
the outbound httpx call so the suite is fully hermetic — no real network.
"""

from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet

from koel.common import crypto
from koel.common.auth import issue_token

ORG_A = "orgA"
ORG_B = "orgB"
SECRET = "sk-super-secret-do-not-leak-999"
# A public literal IP: passes the SSRF guard without any DNS lookup, so create +
# /test stay hermetic (no network) while still exercising URL validation.
PUBLIC_URL = "https://8.8.8.8/v1"


@pytest.fixture(autouse=True)
def _with_master_key(monkeypatch):
    """Configure the Fernet master key so key-bearing creates work, and clear the
    cached instance around every test (the module-level lru_cache is process-wide)."""
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001


def _headers(org_id: str | None = ORG_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


# Captured before any test patches it: the genuine AsyncClient.post. The probe
# and the ASGI test client BOTH use httpx.AsyncClient, so a fake must intercept
# ONLY the outbound "/chat/completions" probe and delegate the test client's own
# requests to the real post.
_REAL_POST = httpx.AsyncClient.post


def _patch_probe(monkeypatch, outcome):  # noqa: ANN001
    """Fake the outbound LLM probe. ``outcome`` is a _FakeResponse to return, or an
    Exception to raise, when the probe hits ``/chat/completions``."""

    async def fake_post(self, url, **kwargs):  # noqa: ANN001, ANN201
        if str(url).endswith("/chat/completions"):
            assert kwargs["json"]["max_tokens"] == 1  # 1-token probe, not a real turn
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return await _REAL_POST(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


async def test_crud_happy_path_never_exposes_the_key(app_client):
    client, _ = app_client
    h = _headers(ORG_A)

    # Create with a real key.
    r = await client.post(
        "/v1/model-connections",
        headers=h,
        json={
            "name": "my-llm",
            "kind": "llm",
            "base_url": PUBLIC_URL,
            "api_key": SECRET,
            "model": "llama-3.3-70b",
            "params": {"temperature": 0.3},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    conn_id = body["id"]
    assert body["api_key_set"] is True
    assert "api_key" not in body  # response model has no key field at all
    assert SECRET not in r.text
    assert body["kind"] == "llm"
    assert body["params"] == {"temperature": 0.3}
    assert body["active"] is True

    # List (org-scoped) includes it; still no key.
    r = await client.get("/v1/model-connections", headers=h)
    assert r.status_code == 200, r.text
    assert conn_id in {c["id"] for c in r.json()}
    assert SECRET not in r.text

    # Optional ?kind= filter.
    r = await client.get("/v1/model-connections?kind=llm", headers=h)
    assert conn_id in {c["id"] for c in r.json()}
    r = await client.get("/v1/model-connections?kind=tts", headers=h)
    assert conn_id not in {c["id"] for c in r.json()}

    # Get by id.
    r = await client.get(f"/v1/model-connections/{conn_id}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["api_key_set"] is True
    assert SECRET not in r.text

    # Patch model — key stays set, never echoed.
    r = await client.patch(
        f"/v1/model-connections/{conn_id}", headers=h, json={"model": "llama-3.1-8b"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "llama-3.1-8b"
    assert r.json()["api_key_set"] is True
    assert SECRET not in r.text

    # Deactivate → active flips false and persists.
    r = await client.post(f"/v1/model-connections/{conn_id}/deactivate", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    r = await client.get(f"/v1/model-connections/{conn_id}", headers=h)
    assert r.json()["active"] is False


async def test_cross_tenant_get_and_list_are_isolated(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(ORG_A),
        json={"name": "a", "kind": "llm", "api_key": "k"},
    )
    conn_id = r.json()["id"]

    # Org B cannot see org A's connection (existence not leaked).
    assert (
        await client.get(f"/v1/model-connections/{conn_id}", headers=_headers(ORG_B))
    ).status_code == 404
    assert (
        await client.get(f"/v1/model-connections/{conn_id}", headers=_headers(ORG_A))
    ).status_code == 200

    # List is org-scoped.
    b_ids = {c["id"] for c in (await client.get("/v1/model-connections", headers=_headers(ORG_B))).json()}
    assert conn_id not in b_ids


async def test_get_and_deactivate_missing_are_404(app_client):
    client, _ = app_client
    assert (
        await client.get("/v1/model-connections/nope", headers=_headers())
    ).status_code == 404
    assert (
        await client.post("/v1/model-connections/nope/deactivate", headers=_headers())
    ).status_code == 404


async def test_create_with_key_but_no_master_key_is_400(app_client, monkeypatch):
    client, _ = app_client
    # Remove the master key configured by the autouse fixture and drop the cache.
    monkeypatch.delenv("KOEL_SECRET_KEY", raising=False)
    crypto._fernet.cache_clear()  # noqa: SLF001

    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "x", "kind": "llm", "api_key": "k"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "secrets_disabled"


async def test_create_without_key_works_when_secrets_disabled(app_client, monkeypatch):
    """No key supplied → no master key needed; create still succeeds."""
    client, _ = app_client
    monkeypatch.delenv("KOEL_SECRET_KEY", raising=False)
    crypto._fernet.cache_clear()  # noqa: SLF001

    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "keyless", "kind": "tts"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["api_key_set"] is False


async def test_bad_kind_is_422(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections", headers=_headers(), json={"name": "x", "kind": "image"}
    )
    assert r.status_code == 422, r.text


async def test_ssrf_base_url_is_422(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "x", "kind": "llm", "base_url": "http://localhost"},
    )
    assert r.status_code == 422, r.text


async def test_requires_auth(app_client):
    client, _ = app_client
    assert (await client.get("/v1/model-connections")).status_code == 401
    assert (
        await client.post("/v1/model-connections", json={"name": "x", "kind": "llm"})
    ).status_code == 401


async def test_test_llm_ok_on_fake_2xx(app_client, monkeypatch):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "llm", "kind": "llm", "base_url": PUBLIC_URL, "api_key": SECRET, "model": "m"},
    )
    conn_id = r.json()["id"]

    _patch_probe(monkeypatch, _FakeResponse(200))

    r = await client.post(f"/v1/model-connections/{conn_id}/test", headers=_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert SECRET not in r.text  # key never surfaces in the probe result


async def test_test_llm_ok_false_on_transport_error(app_client, monkeypatch):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "llm", "kind": "llm", "base_url": PUBLIC_URL, "api_key": SECRET, "model": "m"},
    )
    conn_id = r.json()["id"]

    _patch_probe(monkeypatch, httpx.ConnectError("connection refused"))

    r = await client.post(f"/v1/model-connections/{conn_id}/test", headers=_headers())
    assert r.status_code == 200, r.text  # a failed probe is a normal result, not a 500
    assert r.json()["ok"] is False
    assert SECRET not in r.text


async def test_test_llm_ok_false_on_non_2xx(app_client, monkeypatch):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "llm", "kind": "llm", "base_url": PUBLIC_URL, "api_key": SECRET, "model": "m"},
    )
    conn_id = r.json()["id"]

    _patch_probe(monkeypatch, _FakeResponse(401))

    r = await client.post(f"/v1/model-connections/{conn_id}/test", headers=_headers())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False


async def test_test_tts_is_ok_from_resolve_alone(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/model-connections",
        headers=_headers(),
        json={"name": "tts", "kind": "tts", "api_key": "sk-tts-secret"},
    )
    conn_id = r.json()["id"]

    r = await client.post(f"/v1/model-connections/{conn_id}/test", headers=_headers())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "sk-tts-secret" not in r.text


async def test_test_missing_connection_is_404(app_client):
    client, _ = app_client
    assert (
        await client.post("/v1/model-connections/nope/test", headers=_headers())
    ).status_code == 404
