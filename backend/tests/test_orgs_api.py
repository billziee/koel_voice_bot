"""Admin-only org (tenant) registry CRUD (6.4 Multi-tenant enablement).

Covers the happy path with a platform-admin (unscoped) token, the 403 gate for a
tenant-scoped (non-admin) token, and the unique-slug rejection.
"""

from __future__ import annotations

from koel.common.auth import issue_token


def _admin_headers() -> dict[str, str]:
    # Unscoped principal (org_id=None) == platform admin / "sees all" view.
    return {"Authorization": f"Bearer {issue_token('admin', kind='access', org_id=None)}"}


def _tenant_headers(org_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', org_id=org_id)}"}


async def test_admin_create_list_get_deactivate_happy_path(app_client):
    client, _ = app_client

    # Create
    r = await client.post("/v1/orgs", headers=_admin_headers(), json={"name": "Acme", "slug": "acme"})
    assert r.status_code == 201, r.text
    body = r.json()
    org_id = body["id"]
    assert body["slug"] == "acme"
    assert body["active"] is True
    assert body["created_at"] is not None

    # List includes it
    r = await client.get("/v1/orgs", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert org_id in {o["id"] for o in r.json()}

    # Get by id
    r = await client.get(f"/v1/orgs/{org_id}", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "acme"

    # Deactivate → active flips false
    r = await client.post(f"/v1/orgs/{org_id}/deactivate", headers=_admin_headers())
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False

    # Re-fetch reflects the deactivation (persisted)
    r = await client.get(f"/v1/orgs/{org_id}", headers=_admin_headers())
    assert r.json()["active"] is False


async def test_get_and_deactivate_missing_org_are_404(app_client):
    client, _ = app_client
    assert (await client.get("/v1/orgs/nope", headers=_admin_headers())).status_code == 404
    assert (
        await client.post("/v1/orgs/nope/deactivate", headers=_admin_headers())
    ).status_code == 404


async def test_duplicate_slug_is_rejected_409(app_client):
    client, _ = app_client
    first = await client.post("/v1/orgs", headers=_admin_headers(), json={"name": "Acme", "slug": "acme"})
    assert first.status_code == 201, first.text

    dup = await client.post(
        "/v1/orgs", headers=_admin_headers(), json={"name": "Acme Two", "slug": "acme"}
    )
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "org_slug_exists"


async def test_tenant_scoped_token_cannot_manage_orgs(app_client):
    client, _ = app_client
    tenant = _tenant_headers("acme")

    # Every route is gated: list / create / get / deactivate all 403 for a
    # tenant-scoped (non-admin) principal.
    assert (await client.get("/v1/orgs", headers=tenant)).status_code == 403
    create = await client.post("/v1/orgs", headers=tenant, json={"name": "X", "slug": "x"})
    assert create.status_code == 403
    assert create.json()["code"] == "forbidden"
    assert (await client.get("/v1/orgs/anything", headers=tenant)).status_code == 403
    assert (await client.post("/v1/orgs/anything/deactivate", headers=tenant)).status_code == 403


async def test_orgs_require_auth(app_client):
    client, _ = app_client
    assert (await client.get("/v1/orgs")).status_code == 401
    assert (await client.post("/v1/orgs", json={"name": "X", "slug": "x"})).status_code == 401


async def test_invalid_slug_is_rejected_422(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/orgs", headers=_admin_headers(), json={"name": "Bad", "slug": "Not A Slug!"}
    )
    assert r.status_code == 422, r.text
