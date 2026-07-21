"""POST /v1/auth/refresh and /v1/auth/ws-token (PRD §08 + roadmap M).

Both endpoints mint a fresh token off an already-valid access token: `refresh`
exchanges access-for-access (so the dashboard can silently extend a session
without forcing re-login), `ws-token` mints the short-lived token the socket
upgrade requires. Neither is password- or Google-specific, so they get their
own file rather than living in test_auth_login.py / test_auth_google.py.

These prove:
  * both endpoints require a valid bearer access token (401 without one),
  * both carry over the presenting token's org_id + role verbatim,
  * `refresh` mints an "access" kind token, `ws_token` mints a "ws" kind token
    (so a leaked ws token can never be replayed as a REST access token),
  * `ws-token`'s response reports the configured WS TTL.
"""

from __future__ import annotations

from koel.common.auth import decode_token, issue_token
from koel.voicebot.api import auth as auth_mod


def _disable_token_limit() -> None:
    """Override the shared per-route token-mint limiter so these run in
    isolation from anything else that might saturate it (mirrors the login/
    google limiter overrides in the sibling auth test files)."""
    from koel.voicebot.main import app

    app.dependency_overrides[auth_mod._token_limit] = lambda: None


def _bearer(**issue_kwargs) -> dict[str, str]:
    token = issue_token("op@acme.com", kind="access", **issue_kwargs)
    return {"Authorization": f"Bearer {token}"}


# --- /v1/auth/refresh ---------------------------------------------------------


async def test_refresh_mints_fresh_access_token_carrying_over_scope(app_client):
    client, _ = app_client
    _disable_token_limit()
    r = await client.post(
        "/v1/auth/refresh", headers=_bearer(role="operator", org_id="orgA")
    )
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.sub == "op@acme.com"
    assert claims.role == "operator"
    assert claims.org_id == "orgA"


async def test_refresh_preserves_unscoped_platform_admin(app_client):
    # org_id=None (platform-admin) must round-trip as None, not "None"/dropped.
    client, _ = app_client
    _disable_token_limit()
    r = await client.post("/v1/auth/refresh", headers=_bearer(role="admin", org_id=None))
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.org_id is None
    assert claims.role == "admin"


async def test_refresh_without_bearer_token_is_401(app_client):
    client, _ = app_client
    _disable_token_limit()
    r = await client.post("/v1/auth/refresh")
    assert r.status_code == 401
    assert r.json()["code"] == "missing_token"


async def test_refresh_rejects_a_ws_token(app_client):
    # A ws-kind token must not be usable to mint a fresh access token — refresh
    # requires `CurrentUser` (expected_kind="access"), so a ws token 401s.
    client, _ = app_client
    _disable_token_limit()
    ws_tok = issue_token("op@acme.com", kind="ws", role="operator", org_id="orgA")
    r = await client.post(
        "/v1/auth/refresh", headers={"Authorization": f"Bearer {ws_tok}"}
    )
    assert r.status_code == 401
    assert r.json()["code"] == "wrong_token_kind"


# --- /v1/auth/ws-token ---------------------------------------------------------


async def test_ws_token_mints_ws_kind_token_with_configured_ttl(app_client):
    client, _ = app_client
    _disable_token_limit()
    r = await client.post(
        "/v1/auth/ws-token", headers=_bearer(role="operator", org_id="orgA")
    )
    assert r.status_code == 200
    body = r.json()
    claims = decode_token(body["ws_token"], expected_kind="ws")
    assert claims.sub == "op@acme.com"
    assert claims.role == "operator"
    assert claims.org_id == "orgA"
    assert body["expires_in_seconds"] == auth_mod.get_common_settings().jwt_ws_ttl_seconds


async def test_ws_token_rejected_as_an_access_token(app_client):
    # The minted ws token must not itself pass as an access token elsewhere.
    client, _ = app_client
    _disable_token_limit()
    r = await client.post("/v1/auth/ws-token", headers=_bearer(role="operator"))
    assert r.status_code == 200
    ws_tok = r.json()["ws_token"]
    from koel.common.errors import ApiError

    try:
        decode_token(ws_tok, expected_kind="access")
        raised = False
    except ApiError as exc:
        raised = True
        assert exc.code == "wrong_token_kind"
    assert raised


async def test_ws_token_without_bearer_token_is_401(app_client):
    client, _ = app_client
    _disable_token_limit()
    r = await client.post("/v1/auth/ws-token")
    assert r.status_code == 401
    assert r.json()["code"] == "missing_token"
