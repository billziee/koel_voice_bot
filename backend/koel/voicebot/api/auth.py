"""Login + token endpoints (PRD §08: dashboard behind login, seeded demo creds).

The React dashboard logs in here to obtain an access token for REST calls, then
mints a short-lived WS token immediately before opening the call socket.
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from koel.common.auth import CurrentUser, issue_token
from koel.common.errors import ApiError
from koel.common.ratelimit import rate_limit

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Per-IP throttles (M): brute-force login and token-minting spam.
_login_limit = rate_limit(max_requests=8, window_s=60)
_token_limit = rate_limit(max_requests=30, window_s=60)


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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WsTokenResponse(BaseModel):
    ws_token: str
    expires_in_seconds: int


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(_login_limit)])
async def login(payload: LoginRequest) -> TokenResponse:
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
