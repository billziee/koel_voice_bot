"""Login + token endpoints (PRD §08: dashboard behind login, seeded demo creds).

The React dashboard logs in here to obtain an access token for REST calls, then
mints a short-lived WS token immediately before opening the call socket.
"""

from __future__ import annotations

from typing import Any

import bcrypt
from fastapi import APIRouter, Depends, status
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import select

from koel.common.auth import CurrentUser, issue_token
from koel.common.config import get_common_settings
from koel.common.errors import ApiError
from koel.common.http import make_async_client
from koel.common.logging import get_logger
from koel.common.ratelimit import rate_limit
from koel.db.models import User
from koel.db.session import session_scope

logger = get_logger("koel.voicebot.auth")
router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Per-IP throttles (M): brute-force login and token-minting spam. Distinct names
# keep each quota in its own Redis namespace. Login is pre-auth and brute-force
# sensitive, so it fails CLOSED — a Redis outage rejects rather than silently
# dropping login throttling; token minting is post-auth and stays fail-open.
_login_limit = rate_limit(max_requests=8, window_s=60, name="login", fail_open=False)
_token_limit = rate_limit(max_requests=30, window_s=60, name="token")
# Google login is pre-auth like password login → fails CLOSED in its own namespace.
_google_limit = rate_limit(max_requests=10, window_s=60, name="google_login", fail_open=False)


def _hash(password: str) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())


def _verify(password: str, hashed: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed)
    except ValueError:
        return False


# Constant-work decoy for the unknown-user branch: verifying the presented
# password against a throwaway hash keeps login's timing (and thus its 401)
# indistinguishable between "no such user" and "wrong password", so an attacker
# can't enumerate valid usernames by response time.
_DECOY_HASH: bytes = _hash("koel-nonexistent-user-timing-guard")


def _invalid_credentials() -> ApiError:
    """The one 401 both bad-username and bad-password raise — identical code +
    message so login never leaks WHICH half of the credential was wrong (PRD §08).
    Built fresh per call so each raise carries its own traceback."""
    return ApiError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="invalid_credentials",
        message="Invalid username or password.",
    )


class LoginRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    # Cloudflare Turnstile token from the login widget (6.7). Only consulted when
    # KOEL_TURNSTILE_SECRET_KEY is configured; ignored (optional) otherwise so the
    # dev/demo/CI login path is unchanged. Capped to Turnstile's documented max.
    turnstile_token: str | None = Field(default=None, max_length=2048)


async def _verify_turnstile(token: str | None) -> bool:
    """Verify a Turnstile token with Cloudflare's siteverify API (6.7).

    Fails CLOSED: a missing/empty token, a non-success verdict, or any error
    reaching Cloudflare all return False, so a spoofed or absent challenge is
    rejected rather than waved through (mirrors the login limiter's fail-closed
    posture). Only called when a secret is configured.
    """
    settings = get_common_settings()
    if not token:
        return False
    try:
        async with make_async_client(timeout_s=settings.turnstile_timeout_s) as client:
            resp = await client.post(
                settings.turnstile_verify_url,
                data={"secret": settings.turnstile_secret_key, "response": token},
            )
            resp.raise_for_status()
            return bool(resp.json().get("success") is True)
    except Exception:  # noqa: BLE001 — any verification failure ⇒ reject (fail closed)
        logger.warning("turnstile_verify_failed", exc_info=True)
        return False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WsTokenResponse(BaseModel):
    ws_token: str
    expires_in_seconds: int


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(_login_limit)])
async def login(payload: LoginRequest) -> TokenResponse:
    # Bot-challenge (6.7): when a Turnstile secret is configured, a valid token is
    # required BEFORE the credential check, so brute-force attempts can't even reach
    # bcrypt without first clearing the challenge. Skipped when unconfigured (demo).
    if get_common_settings().turnstile_secret_key:
        if not await _verify_turnstile(payload.turnstile_token):
            raise ApiError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="turnstile_failed",
                message="Human verification failed. Please retry the challenge.",
            )
    # Look the user up in the Users table (6.4 follow-up). The token is minted
    # carrying THIS user's org_id + role, so a logged-in operator is scoped to
    # their tenant. org_id=None (platform-admin, e.g. the demo user) mints the
    # unscoped "sees all" principal — the convention every scoped router honours.
    async with session_scope() as db:
        user = (
            await db.execute(select(User).where(User.username == payload.username))
        ).scalar_one_or_none()
    if user is None or not user.active or user.password_hash is None:
        # Decoy verify keeps timing constant so an unknown username can't be told
        # apart from a wrong password; both raise the identical 401. A passwordless
        # (Google) row also lands here — it must never authenticate via password.
        _verify(payload.password, _DECOY_HASH)
        raise _invalid_credentials()
    if not _verify(payload.password, user.password_hash):
        raise _invalid_credentials()
    # Subject = payload.username: we matched `User.username == payload.username`, so
    # it equals user.username but is a definite str (user.username is now nullable).
    return TokenResponse(
        access_token=issue_token(
            payload.username, kind="access", role=user.role, org_id=user.org_id
        )
    )


class GoogleLoginRequest(BaseModel):
    # The Google ID token (a JWT) returned by Google Identity Services — GIS calls
    # this field `credential`. Capped generously; real ID tokens are ~1KB.
    credential: str = Field(max_length=8192)


async def _verify_google_id_token(credential: str) -> dict[str, Any] | None:
    """Verify a Google ID token against Google's JWKS and return its claims.

    Returns None on ANY failure (fail closed): no client id configured, malformed
    token, JWKS fetch error, unknown signing key, bad signature, wrong audience,
    non-Google issuer, or expiry. jose enforces the signature (RS256), ``aud`` ==
    our client id, the issuer, and ``exp``; the caller additionally requires
    ``email_verified``. We never hand-verify crypto — jose owns that.
    """
    settings = get_common_settings()
    if not settings.google_client_id:
        return None
    try:
        kid = jwt.get_unverified_header(credential).get("kid")
    except JWTError:
        return None
    if not kid:
        return None
    try:
        async with make_async_client(timeout_s=settings.google_token_timeout_s) as client:
            resp = await client.get(settings.google_jwks_url)
            resp.raise_for_status()
            jwks = resp.json()
        # Key selection is inside the try so a malformed JWKS body (not a dict /
        # no "keys") fails CLOSED (→ None → 401) instead of raising a 500.
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    except Exception:  # noqa: BLE001 — JWKS fetch/parse failure ⇒ reject (fail closed)
        logger.warning("google_jwks_fetch_failed", exc_info=True)
        return None
    if key is None:
        return None
    try:
        return jwt.decode(
            credential,
            key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=["https://accounts.google.com", "accounts.google.com"],
        )
    except JWTError:
        logger.info("google_id_token_invalid", exc_info=True)
        return None


@router.post("/google", response_model=TokenResponse, dependencies=[Depends(_google_limit)])
async def google_login(payload: GoogleLoginRequest) -> TokenResponse:
    """Log in with a Google ID token (allowlist model).

    Disabled unless ``KOEL_GOOGLE_CLIENT_ID`` is set (403 ``google_login_disabled``
    — the frontend only shows the button when its own client id is configured, so
    the demo/CI path never calls this). When enabled: verify the ID token, require
    a verified email, then resolve that email against the PRE-PROVISIONED users
    allowlist. An unknown or unverified email raises the SAME 401 as a bad
    password — Google login never reveals whether an email is on the allowlist.
    The token minted carries the allowlisted row's ``org_id`` + ``role``.
    """
    settings = get_common_settings()
    if not settings.google_client_id:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="google_login_disabled",
            message="Google Sign-In is not enabled on this deployment.",
        )
    claims = await _verify_google_id_token(payload.credential)
    if claims is None or claims.get("email_verified") is not True:
        raise _invalid_credentials()
    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "")
    if not email or not sub:
        raise _invalid_credentials()
    async with session_scope() as db:
        # Row-lock the read (FOR UPDATE on Postgres; a no-op on SQLite tests) so two
        # concurrent first-logins for the same email with DIFFERENT subs can't both
        # take the "bind" branch — the second sees the bound sub and is rejected.
        user = (
            await db.execute(select(User).where(User.email == email).with_for_update())
        ).scalar_one_or_none()
        # Allowlist: only a pre-provisioned, active row may sign in.
        if user is None or not user.active:
            raise _invalid_credentials()
        # Reject an email that collides with a NON-Google row (defence in depth:
        # a password user's email should never be assumable via Google).
        if user.auth_provider != "google":
            raise _invalid_credentials()
        # Bind the stable Google subject on first login; verify it thereafter so a
        # re-used email (rare, but Google allows deletion + reassignment within a
        # Workspace) can't silently inherit a prior person's access.
        if user.google_sub is None:
            user.google_sub = sub
        elif user.google_sub != sub:
            raise _invalid_credentials()
        # `email` (the definite, verified, lowercased str we matched on) == user.email
        # but is typed str, not str | None. role/org_id captured before the session closes.
        role, org_id = user.role, user.org_id
    return TokenResponse(
        access_token=issue_token(email, kind="access", role=role, org_id=org_id)
    )


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(_token_limit)])
async def refresh(user: CurrentUser) -> TokenResponse:
    """Exchange a still-valid access token for a fresh one (M).

    With a short access TTL, the dashboard refreshes silently instead of forcing
    re-login. org_id/role are carried over from the presented token.
    """
    return TokenResponse(
        access_token=issue_token(user.sub, kind="access", role=user.role, org_id=user.org_id)
    )


@router.post("/ws-token", response_model=WsTokenResponse, dependencies=[Depends(_token_limit)])
async def ws_token(user: CurrentUser) -> WsTokenResponse:
    """Mint a short-lived token for the WebSocket upgrade (PRD §08)."""
    from koel.common.config import get_common_settings

    settings = get_common_settings()
    return WsTokenResponse(
        ws_token=issue_token(user.sub, kind="ws", role=user.role, org_id=user.org_id),
        expires_in_seconds=settings.jwt_ws_ttl_seconds,
    )
