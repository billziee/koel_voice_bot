"""Google Sign-In (allowlist model) — POST /v1/auth/google.

The endpoint verifies a Google ID token, requires a verified email, and resolves
that email against the PRE-PROVISIONED users allowlist, minting a token carrying
the row's org_id + role. These tests monkeypatch the token verifier (so no
network / no real Google key) and exercise the endpoint's allowlist + scoping
logic, plus the config-driven allowlist seeding. They prove:

  * disabled (403) unless a client id is configured — the demo path is untouched,
  * an allowlisted, verified email mints a token scoped to that row's org/role,
    and binds google_sub on first login,
  * unknown / unverified / inactive / sub-mismatch emails all get the SAME 401
    (no allowlist enumeration),
  * a passwordless (Google) row can never authenticate via the password endpoint,
  * the KOEL_GOOGLE_ALLOWLIST CSV parses + seeds idempotently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select

from koel.common.auth import decode_token
from koel.common.config import CommonSettings
from koel.db.models import User
from koel.db.seed_users import parse_google_allowlist, seed_google_allowlist
from koel.voicebot.api import auth as auth_mod

_CLIENT_ID = "test-client.apps.googleusercontent.com"


def _enable_google(monkeypatch, *, client_id: str = _CLIENT_ID) -> None:
    """Point the auth module at settings with a Google client id configured."""
    settings = CommonSettings(google_client_id=client_id)
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: settings)


def _disable_google_limit() -> None:
    from koel.voicebot.main import app

    app.dependency_overrides[auth_mod._google_limit] = lambda: None


def _fake_verifier(claims: dict | None) -> Callable[[str], Awaitable[dict | None]]:
    async def _verify(_credential: str) -> dict | None:
        return claims

    return _verify


async def _seed_google_user(
    maker,
    *,
    email: str,
    org_id: str | None = None,
    role: str = "operator",
    active: bool = True,
    google_sub: str | None = None,
) -> None:
    async with maker() as s:
        s.add(
            User(
                email=email.lower(),
                username=None,
                password_hash=None,
                auth_provider="google",
                org_id=org_id,
                role=role,
                active=active,
                google_sub=google_sub,
            )
        )
        await s.commit()


# --- endpoint: disabled unless configured -----------------------------------


async def test_google_login_disabled_returns_403(app_client):
    # Default settings have no client id ⇒ Google login is off, demo path intact.
    client, _ = app_client
    _disable_google_limit()
    r = await client.post("/v1/auth/google", json={"credential": "x.y.z"})
    assert r.status_code == 403
    assert r.json()["code"] == "google_login_disabled"


# --- endpoint: happy path ----------------------------------------------------


async def test_allowlisted_email_mints_scoped_token_and_binds_sub(
    app_client, test_db, monkeypatch
):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    await _seed_google_user(test_db, email="op@acme.com", org_id="orgA", role="operator")
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "op@acme.com", "email_verified": True, "sub": "google-sub-123"}),
    )

    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.sub == "op@acme.com"
    assert claims.org_id == "orgA"
    assert claims.role == "operator"

    # google_sub is bound on first login (was None), matched thereafter.
    async with test_db() as s:
        user = (await s.execute(select(User).where(User.email == "op@acme.com"))).scalar_one()
        assert user.google_sub == "google-sub-123"


async def test_platform_admin_google_user_is_unscoped(app_client, test_db, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    await _seed_google_user(test_db, email="ceo@acme.com", org_id=None, role="admin")
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "ceo@acme.com", "email_verified": True, "sub": "s-admin"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 200
    claims = decode_token(r.json()["access_token"], expected_kind="access")
    assert claims.org_id is None  # "sees all"
    assert claims.role == "admin"


# --- endpoint: all rejections share the one 401 ------------------------------


async def test_unknown_email_is_401(app_client, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "stranger@nowhere.com", "email_verified": True, "sub": "s"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_unverified_email_is_401(app_client, test_db, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    await _seed_google_user(test_db, email="op@acme.com", org_id="orgA")
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "op@acme.com", "email_verified": False, "sub": "s"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401


async def test_invalid_token_is_401(app_client, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    monkeypatch.setattr(auth_mod, "_verify_google_id_token", _fake_verifier(None))
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401


async def test_inactive_google_user_is_401(app_client, test_db, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    await _seed_google_user(test_db, email="off@acme.com", org_id="orgA", active=False)
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "off@acme.com", "email_verified": True, "sub": "s"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401


async def test_google_sub_mismatch_is_401(app_client, test_db, monkeypatch):
    # Row already bound to one Google subject; a token with a different sub (email
    # reassigned to a new person) must not inherit the prior access.
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    await _seed_google_user(test_db, email="op@acme.com", org_id="orgA", google_sub="sub-A")
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "op@acme.com", "email_verified": True, "sub": "sub-B"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401


# --- a passwordless row can never log in via the password endpoint -----------


async def test_passwordless_row_cannot_use_password_login(app_client, test_db):
    client, _ = app_client
    from koel.voicebot.main import app

    app.dependency_overrides[auth_mod._login_limit] = lambda: None
    # A row with a username but NO password_hash (defence-in-depth guard).
    async with test_db() as s:
        s.add(
            User(
                username="ghost",
                password_hash=None,
                auth_provider="google",
                email="ghost@acme.com",
                org_id="orgA",
                role="operator",
            )
        )
        await s.commit()
    r = await client.post("/v1/auth/login", json={"username": "ghost", "password": "anything"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


# --- verifier fails closed (no network) --------------------------------------


async def test_verify_returns_none_when_client_id_unset(monkeypatch):
    monkeypatch.setattr(
        auth_mod, "get_common_settings", lambda: CommonSettings(google_client_id="")
    )
    assert await auth_mod._verify_google_id_token("x.y.z") is None


async def test_verify_returns_none_on_malformed_token(monkeypatch):
    _enable_google(monkeypatch)
    assert await auth_mod._verify_google_id_token("not-a-jwt") is None


# --- allowlist parsing + seeding ---------------------------------------------


def test_parse_google_allowlist_variants():
    assert parse_google_allowlist("") == ()
    assert parse_google_allowlist("a@x.com") == (("a@x.com", None, "operator"),)
    assert parse_google_allowlist("A@X.com::admin") == (("a@x.com", None, "admin"),)
    assert parse_google_allowlist("a@x.com:orgA:operator, b@y.com:orgB") == (
        ("a@x.com", "orgA", "operator"),
        ("b@y.com", "orgB", "operator"),
    )
    # Malformed entries (no @) + blanks are skipped, not fatal.
    assert parse_google_allowlist("garbage, ,c@z.com") == (("c@z.com", None, "operator"),)
    # role is case-insensitive against the allowed human set...
    assert parse_google_allowlist("a@x.com::Admin") == (("a@x.com", None, "admin"),)
    # ...and a privileged/unknown role ("service" is the machine principal) is
    # coerced to least-privilege "operator" — a typo must never escalate.
    assert parse_google_allowlist("svc@x.com::service") == (("svc@x.com", None, "operator"),)
    assert parse_google_allowlist("t@x.com::superuser") == (("t@x.com", None, "operator"),)


async def test_seed_google_allowlist_revokes_removed_email(test_db):
    # Provision two, then drop one from the CSV → the dropped row is DEACTIVATED
    # (removal from the env is a real revocation), the kept one stays active.
    async with test_db() as s:
        await seed_google_allowlist(s, "keep@acme.com::admin, drop@acme.com:orgA:operator")
        await s.commit()
    async with test_db() as s:
        await seed_google_allowlist(s, "keep@acme.com::admin")
        await s.commit()
    async with test_db() as s:
        dropped = (await s.execute(select(User).where(User.email == "drop@acme.com"))).scalar_one()
        kept = (await s.execute(select(User).where(User.email == "keep@acme.com"))).scalar_one()
        assert dropped.active is False
        assert kept.active is True
    # Re-adding the email re-activates it (deactivation is reversible).
    async with test_db() as s:
        await seed_google_allowlist(s, "keep@acme.com::admin, drop@acme.com:orgA:operator")
        await s.commit()
    async with test_db() as s:
        dropped = (await s.execute(select(User).where(User.email == "drop@acme.com"))).scalar_one()
        assert dropped.active is True


async def test_seed_google_allowlist_empty_revokes_all(test_db):
    # An empty allowlist ⇒ nobody may sign in with Google: every Google row is
    # deactivated (defence against leaving the env unset while rows linger).
    async with test_db() as s:
        await seed_google_allowlist(s, "a@acme.com::admin, b@acme.com:orgA:operator")
        await s.commit()
    async with test_db() as s:
        await seed_google_allowlist(s, "")
        await s.commit()
    async with test_db() as s:
        rows = (
            await s.execute(select(User).where(User.auth_provider == "google"))
        ).scalars().all()
        assert rows and all(r.active is False for r in rows)


async def test_seed_google_allowlist_inserts_then_syncs_idempotently(test_db):
    async with test_db() as s:
        inserted = await seed_google_allowlist(s, "ceo@acme.com::admin, op@acme.com:orgA:operator")
        await s.commit()
    assert inserted == 2

    # Re-run with a changed org/role: no new inserts, existing row re-synced.
    async with test_db() as s:
        reinserted = await seed_google_allowlist(s, "ceo@acme.com:orgB:operator")
        await s.commit()
    assert reinserted == 0

    async with test_db() as s:
        ceo = (await s.execute(select(User).where(User.email == "ceo@acme.com"))).scalar_one()
        assert ceo.org_id == "orgB"
        assert ceo.role == "operator"
        assert ceo.auth_provider == "google"
        assert ceo.password_hash is None
        assert ceo.username is None
