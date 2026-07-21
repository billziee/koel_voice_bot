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

import time
from collections.abc import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
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


async def test_empty_email_after_verification_is_401(app_client, monkeypatch):
    # A verified token whose claims resolve to an empty email must not proceed
    # to any DB lookup — same 401 as any other rejection (no crash on a blank key).
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "", "email_verified": True, "sub": "s"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_empty_sub_after_verification_is_401(app_client, monkeypatch):
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "op@acme.com", "email_verified": True, "sub": ""}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


async def test_google_email_colliding_with_password_row_is_401(app_client, test_db, monkeypatch):
    # Defence in depth: a row provisioned for PASSWORD login (auth_provider
    # defaults to "password") must never be reachable via Google, even when a
    # verified Google token presents that row's exact email.
    client, _ = app_client
    _enable_google(monkeypatch)
    _disable_google_limit()
    async with test_db() as s:
        s.add(
            User(
                username="op_pw",
                password_hash=auth_mod._hash("pw"),
                email="shared@acme.com",
                org_id="orgA",
                role="operator",
            )
        )
        await s.commit()
    monkeypatch.setattr(
        auth_mod,
        "_verify_google_id_token",
        _fake_verifier({"email": "shared@acme.com", "email_verified": True, "sub": "s"}),
    )
    r = await client.post("/v1/auth/google", json={"credential": "any"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_credentials"


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


# --- verifier: real RS256 signature + JWKS round trip (no network) -----------
#
# These exercise `_verify_google_id_token` itself end-to-end against a throwaway
# keypair standing in for Google's — jose owns all the crypto (signature, aud,
# iss, exp); we only fake the JWKS HTTP fetch via `make_async_client`, mirroring
# the `test_turnstile.py` fake-client pattern.

_RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _RSA_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = _RSA_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()
_SIGNING_KID = "test-signing-key-1"


def _id_token(*, kid: str | None = _SIGNING_KID, **claim_overrides: object) -> str:
    """Sign a real RS256 Google-shaped ID token with our throwaway keypair."""
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": _CLIENT_ID,
        "sub": "google-sub-xyz",
        "email": "op@acme.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(claim_overrides)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers=headers)


def _jwks(*, kid: str = _SIGNING_KID) -> dict:
    key = jwk.construct(_PUBLIC_PEM, algorithm="RS256").to_dict()
    key["kid"] = kid
    return {"keys": [key]}


class _FakeJwksResp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeJwksClient:
    """Stands in for the httpx client `make_async_client` returns (GET-only, JWKS)."""

    def __init__(self, *, payload: object = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise = raise_exc

    async def __aenter__(self) -> _FakeJwksClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> _FakeJwksResp:
        if self._raise is not None:
            raise self._raise
        return _FakeJwksResp(self._payload)


def _fake_jwks_factory(**kwargs: object):
    def _make(**_ignored: object) -> _FakeJwksClient:
        return _FakeJwksClient(**kwargs)
    return _make


async def test_verify_returns_none_when_header_has_no_kid(monkeypatch):
    # No `kid` in the header means we can't select a JWKS key — reject before
    # ever touching the network.
    _enable_google(monkeypatch)
    token = _id_token(kid=None)
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_fails_closed_when_jwks_fetch_errors(monkeypatch):
    _enable_google(monkeypatch)
    token = _id_token()
    monkeypatch.setattr(
        auth_mod, "make_async_client",
        _fake_jwks_factory(raise_exc=httpx.ConnectError("google unreachable")),
    )
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_fails_closed_on_malformed_jwks_body(monkeypatch):
    # A JWKS response that isn't the expected {"keys": [...]} shape (e.g. a bare
    # list) must reject via the fail-closed except, never a 500.
    _enable_google(monkeypatch)
    token = _id_token()
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=[]))
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_returns_none_when_kid_not_in_jwks(monkeypatch):
    # JWKS fetched fine but doesn't contain OUR token's kid (mid key-rotation).
    _enable_google(monkeypatch)
    token = _id_token(kid="signing-key-that-google-rotated-away")
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=_jwks()))
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_succeeds_on_valid_signature_and_returns_claims(monkeypatch):
    _enable_google(monkeypatch)
    token = _id_token(email="real-user@acme.com", sub="sub-real-1")
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=_jwks()))
    claims = await auth_mod._verify_google_id_token(token)
    assert claims is not None
    assert claims["email"] == "real-user@acme.com"
    assert claims["sub"] == "sub-real-1"
    assert claims["email_verified"] is True


async def test_verify_rejects_expired_token(monkeypatch):
    # jose's own `exp` check → JWTError → fail closed (not a hand-rolled check).
    _enable_google(monkeypatch)
    now = int(time.time())
    token = _id_token(iat=now - 7200, exp=now - 3600)
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=_jwks()))
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_rejects_wrong_audience(monkeypatch):
    _enable_google(monkeypatch)
    token = _id_token(aud="someone-elses-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=_jwks()))
    assert await auth_mod._verify_google_id_token(token) is None


async def test_verify_rejects_wrong_issuer(monkeypatch):
    _enable_google(monkeypatch)
    token = _id_token(iss="https://not-google.example.com")
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_jwks_factory(payload=_jwks()))
    assert await auth_mod._verify_google_id_token(token) is None


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
