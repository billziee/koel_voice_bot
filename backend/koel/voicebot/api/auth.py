"""Login + token endpoints (PRD §08: dashboard behind login, seeded demo creds).

The React dashboard logs in here to obtain an access token for REST calls, then
mints a short-lived WS token immediately before opening the call socket.
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, status
from pydantic import BaseModel

from koel.common.auth import CurrentUser, issue_token
from koel.common.errors import ApiError

router = APIRouter(prefix="/v1/auth", tags=["auth"])


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
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WsTokenResponse(BaseModel):
    ws_token: str
    expires_in_seconds: int


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    hashed = _DEMO_USERS.get(payload.username)
    if not hashed or not _verify(payload.password, hashed):
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_credentials",
            message="Invalid username or password.",
        )
    return TokenResponse(access_token=issue_token(payload.username, kind="access"))


@router.post("/ws-token", response_model=WsTokenResponse)
async def ws_token(user: CurrentUser) -> WsTokenResponse:
    """Mint a short-lived token for the WebSocket upgrade (PRD §08)."""
    from koel.common.config import get_common_settings

    settings = get_common_settings()
    return WsTokenResponse(
        ws_token=issue_token(user.sub, kind="ws", role=user.role),
        expires_in_seconds=settings.jwt_ws_ttl_seconds,
    )
