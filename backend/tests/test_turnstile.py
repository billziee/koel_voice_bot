"""Cloudflare Turnstile bot-challenge on /v1/auth/login (roadmap 6.7).

Two levels: the ``_verify_turnstile`` siteverify helper against a fake Cloudflare
(so no network), and the /login gating that consumes it. All login tests disable
the per-IP login rate limiter (a shared module-level closure that other tests
saturate) so they exercise the Turnstile path in isolation.
"""

from __future__ import annotations

import types

import httpx

from koel.voicebot.api import auth as auth_mod


def _settings(secret: str = ""):
    return types.SimpleNamespace(
        turnstile_secret_key=secret,
        turnstile_verify_url="https://challenges.cloudflare.com/turnstile/v0/siteverify",
        turnstile_timeout_s=5.0,
    )


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for the httpx client `make_async_client` returns (an async ctx mgr)."""

    def __init__(self, *, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload or {}
        self._raise = raise_exc

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, data: dict | None = None) -> _FakeResp:
        if self._raise is not None:
            raise self._raise
        return _FakeResp(self._payload)


def _fake_client_factory(**kwargs):
    def _make(**_ignored):
        return _FakeClient(**kwargs)
    return _make


# --- _verify_turnstile unit (fails CLOSED on anything but an explicit success) ---

async def test_verify_false_without_token(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    assert await auth_mod._verify_turnstile(None) is False
    assert await auth_mod._verify_turnstile("") is False


async def test_verify_true_on_cloudflare_success(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_client_factory(payload={"success": True}))
    assert await auth_mod._verify_turnstile("tok") is True


async def test_verify_false_on_cloudflare_rejection(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(
        auth_mod, "make_async_client",
        _fake_client_factory(payload={"success": False, "error-codes": ["invalid-input-response"]}),
    )
    assert await auth_mod._verify_turnstile("tok") is False


async def test_verify_fails_closed_on_network_error(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(
        auth_mod, "make_async_client",
        _fake_client_factory(raise_exc=httpx.ConnectError("cloudflare unreachable")),
    )
    assert await auth_mod._verify_turnstile("tok") is False


# --- /login gating ---

def _disable_login_limit():
    """Override the per-route login rate limiter so Turnstile tests don't collide
    with the shared module-level limiter that other tests saturate."""
    from koel.voicebot.main import app
    app.dependency_overrides[auth_mod._login_limit] = lambda: None


async def test_login_unchanged_when_turnstile_unconfigured(app_client, monkeypatch):
    client, _ = app_client
    _disable_login_limit()
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings(""))  # no secret
    r = await client.post("/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
    assert r.status_code == 200
    assert r.json()["access_token"]


async def test_login_rejects_missing_token_when_configured(app_client, monkeypatch):
    client, _ = app_client
    _disable_login_limit()
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    r = await client.post("/v1/auth/login", json={"username": "demo", "password": "koel-demo"})
    assert r.status_code == 403
    assert r.json()["code"] == "turnstile_failed"


async def test_login_rejects_invalid_token(app_client, monkeypatch):
    client, _ = app_client
    _disable_login_limit()
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_client_factory(payload={"success": False}))
    r = await client.post(
        "/v1/auth/login",
        json={"username": "demo", "password": "koel-demo", "turnstile_token": "bad"},
    )
    assert r.status_code == 403


async def test_login_succeeds_with_valid_token(app_client, monkeypatch):
    client, _ = app_client
    _disable_login_limit()
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_client_factory(payload={"success": True}))
    r = await client.post(
        "/v1/auth/login",
        json={"username": "demo", "password": "koel-demo", "turnstile_token": "good"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


async def test_valid_token_but_wrong_password_still_401(app_client, monkeypatch):
    # Turnstile clears the challenge but must NOT bypass credential checks.
    client, _ = app_client
    _disable_login_limit()
    monkeypatch.setattr(auth_mod, "get_common_settings", lambda: _settings("secret"))
    monkeypatch.setattr(auth_mod, "make_async_client", _fake_client_factory(payload={"success": True}))
    r = await client.post(
        "/v1/auth/login",
        json={"username": "demo", "password": "WRONG", "turnstile_token": "good"},
    )
    assert r.status_code == 401


# --- Production startup guard (6.7 review finding #3) ---

def _cfg(*, production: bool, secret: str):
    return types.SimpleNamespace(is_production=production, turnstile_secret_key=secret)


def test_prod_without_turnstile_secret_warns():
    from koel.voicebot.main import warn_if_turnstile_unenforced
    assert warn_if_turnstile_unenforced(_cfg(production=True, secret="")) is True


def test_prod_with_secret_is_silent():
    from koel.voicebot.main import warn_if_turnstile_unenforced
    assert warn_if_turnstile_unenforced(_cfg(production=True, secret="sk_live")) is False


def test_non_prod_never_warns():
    from koel.voicebot.main import warn_if_turnstile_unenforced
    assert warn_if_turnstile_unenforced(_cfg(production=False, secret="")) is False
