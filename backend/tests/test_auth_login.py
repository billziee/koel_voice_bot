"""DB-backed login mints per-user org scope (6.4 follow-up).

``/v1/auth/login`` now looks the user up in the Users table and mints a token
carrying that user's ``org_id`` + ``role`` — replacing the old
unscoped-for-everyone token. This proves:

  * an org-scoped operator's token carries ``org_id == "orgA"`` + role operator,
  * the platform-admin (``demo``) token carries ``org_id is None`` (sees all),
  * an unknown user and a wrong password return the SAME 401 (no user
    enumeration via response body),
  * an inactive user cannot log in,
  * the demo login still works with Turnstile unconfigured.

Users are seeded into the in-memory DB by the ``test_db`` fixture (via
``koel.db.seed_users``), so these exercise the real DB read + bcrypt verify.
"""

from __future__ import annotations

from koel.common.auth import decode_token
from koel.db.seed_users import ORG_A_ID
from koel.voicebot.api import auth as auth_mod
from tests.conftest import seed_user


def _disable_login_limit() -> None:
    """Override the per-route login limiter (shared module-level closure that
    other tests saturate) so these run in isolation. Cleared by app_client."""
    from koel.voicebot.main import app

    app.dependency_overrides[auth_mod._login_limit] = lambda: None


async def test_org_operator_login_carries_tenant_scope(app_client):
    client, _ = app_client
    _disable_login_limit()
    r = await client.post(
        "/v1/auth/login", json={"username": "orga_operator", "password": "orga-demo"}
    )
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.sub == "orga_operator"
    assert claims.org_id == ORG_A_ID == "orgA"
    assert claims.role == "operator"


async def test_platform_admin_login_is_unscoped(app_client):
    client, _ = app_client
    _disable_login_limit()
    r = await client.post("/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.sub == "demo"
    assert claims.org_id is None  # the unscoped "sees all" principal
    assert claims.role == "admin"


async def test_unknown_user_and_bad_password_are_the_same_401(app_client):
    client, _ = app_client
    _disable_login_limit()
    unknown = await client.post("/v1/auth/login", json={"username": "nope", "password": "x"})
    bad_pw = await client.post("/v1/auth/login", json={"username": "demo", "password": "WRONG"})
    assert unknown.status_code == 401
    assert bad_pw.status_code == 401
    # Identical envelope — login never leaks WHICH half of the credential failed.
    assert unknown.json()["code"] == bad_pw.json()["code"] == "invalid_credentials"
    assert unknown.json()["message"] == bad_pw.json()["message"]


async def test_inactive_user_cannot_login(app_client, test_db):
    client, _ = app_client
    _disable_login_limit()
    await seed_user(
        test_db, username="disabled_op", password="pw", org_id="orgA", active=False
    )
    r = await client.post("/v1/auth/login", json={"username": "disabled_op", "password": "pw"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_demo_login_works_with_turnstile_unset(app_client):
    # Turnstile is configurable via KOEL_TURNSTILE_SECRET_KEY; unset (the default)
    # leaves the demo login path unchanged — no challenge required.
    client, _ = app_client
    _disable_login_limit()
    assert not auth_mod.get_common_settings().turnstile_secret_key
    r = await client.post("/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
    assert r.status_code == 200
    assert r.json()["access_token"]


# --- _verify fails closed on a malformed stored hash --------------------------


def test_verify_returns_false_on_malformed_hash():
    # bcrypt.checkpw raises ValueError (not just False) when the stored hash
    # isn't a well-formed bcrypt hash (bad salt/prefix). _verify must swallow
    # that and report "no match" rather than let a 500 leak through — a corrupt
    # row must fail the login, never crash it.
    assert auth_mod._verify("any-password", b"not-a-valid-bcrypt-hash") is False


def test_verify_still_true_for_a_genuinely_matching_hash():
    hashed = auth_mod._hash("correct horse battery staple")
    assert auth_mod._verify("correct horse battery staple", hashed) is True
    assert auth_mod._verify("wrong", hashed) is False
