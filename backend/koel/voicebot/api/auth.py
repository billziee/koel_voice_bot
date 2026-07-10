"""Login + token endpoints (PRD §08: dashboard behind login, seeded demo creds).

The React dashboard logs in here to obtain an access token for REST calls, then
mints a short-lived WS token immediately before opening the call socket.
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from koel.common.auth import CurrentUser, issue_token
from koel.common.config import get_common_settings
from koel.common.errors import ApiError
from koel.common.http import make_async_client
from koel.common.logging import get_logger
from koel.common.ratelimit import rate_limit

logger = get_logger("koel.voicebot.auth")
router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Per-IP throttles (M): brute-force login and token-minting spam. Distinct names
# keep each quota in its own Redis namespace. Login is pre-auth and brute-force
# sensitive, so it fails CLOSED — a Redis outage rejects rather than silently
# dropping login throttling; token minting is post-auth and stays fail-open.
_login_limit = rate_limit(max_requests=8, window_s=60, name="login", fail_open=False)
_token_limit = rate_limit(max_requests=30, window_s=60, name="token")


def _hash(password: str) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())


def _verify(password: str, hashed: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed)
    except ValueError:
        return False


# Seeded demo credentials (acceptable for showcases per PRD §08). Hashed at
# import so no plaintext password sits in memory beyond startup.
_DEMO_USERS: dict[str, bytes] = {"demo": _hash("koel-demo")}


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
    hashed = _DEMO_USERS.get(payload.username)
    if not hashed or not _verify(payload.password, hashed):
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_credentials",
            message="Invalid username or password.",
        )
    return TokenResponse(access_token=issue_token(payload.username, kind="access"))


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
