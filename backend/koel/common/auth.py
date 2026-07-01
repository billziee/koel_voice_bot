"""JWT authentication for REST and the WebSocket upgrade.

PRD §08 (Authentication & Access Control):
  - No open endpoints, even in MVP.
  - All REST endpoints require a JWT bearer token.
  - The WebSocket handshake must present a valid short-lived JWT before the
    connection upgrades; unauthenticated socket attempts are rejected with 401.

Two token flavours are minted:
  - access token  — for REST calls, longer TTL.
  - ws token      — short-lived, minted right before opening the socket so a
    leaked dashboard token can't be replayed to spin up inference sessions.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from fastapi import Depends, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from koel.common.config import CommonSettings, get_common_settings
from koel.common.errors import ApiError

TokenKind = Literal["access", "ws"]


class TokenClaims(BaseModel):
    sub: str  # user id / demo username
    kind: TokenKind
    role: str = "operator"
    org_id: str | None = None  # tenant scope (AF); None = single-tenant pilot
    exp: int
    iat: int
    jti: str


def _now() -> int:
    return int(time.time())


def issue_token(
    subject: str,
    *,
    kind: TokenKind,
    role: str = "operator",
    org_id: str | None = None,
    settings: CommonSettings | None = None,
) -> str:
    settings = settings or get_common_settings()
    ttl = settings.jwt_access_ttl_seconds if kind == "access" else settings.jwt_ws_ttl_seconds
    issued = _now()
    claims = {
        "sub": subject,
        "kind": kind,
        "role": role,
        "org_id": org_id,
        "iat": issued,
        "exp": issued + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(
    token: str,
    *,
    expected_kind: TokenKind,
    settings: CommonSettings | None = None,
) -> TokenClaims:
    settings = settings or get_common_settings()
    try:
        raw = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        claims = TokenClaims(**raw)
    except (JWTError, ValueError) as exc:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_token",
            message="Token is invalid or expired.",
        ) from exc
    if claims.kind != expected_kind:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="wrong_token_kind",
            message=f"Expected a '{expected_kind}' token.",
        )
    return claims


# --- REST dependency ---------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


async def require_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenClaims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="missing_token",
            message="Authorization: Bearer <token> required.",
        )
    return decode_token(credentials.credentials, expected_kind="access")


CurrentUser = Annotated[TokenClaims, Depends(require_access_token)]


async def require_service_principal(claims: CurrentUser) -> TokenClaims:
    """Only VB_Svc (role=service) may call the internal AI/CRM services.

    Prevents a leaked operator dashboard token from driving Bedrock/SageMaker
    inference directly (cost abuse) or probing the CRM (PRD §08).
    """
    if claims.role != "service":
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="This endpoint requires a service principal.",
        )
    return claims


ServicePrincipal = Annotated[TokenClaims, Depends(require_service_principal)]


# --- WebSocket guard ---------------------------------------------------------

WS_TOKEN_SUBPROTOCOL_PREFIX = "koel.token."
WS_SUBPROTOCOL = "koel.v1"


def websocket_token(websocket: WebSocket) -> str | None:
    """Extract the WS JWT, preferring transports that keep it out of the URL (M).

    Order: Sec-WebSocket-Protocol (``koel.token.<jwt>``) → Authorization: Bearer
    → ``?token=`` query param (deprecated — leaks into access logs; kept only for
    backward compatibility).
    """
    protocols = websocket.headers.get("sec-websocket-protocol", "")
    for proto in (p.strip() for p in protocols.split(",")):
        if proto.startswith(WS_TOKEN_SUBPROTOCOL_PREFIX):
            return proto[len(WS_TOKEN_SUBPROTOCOL_PREFIX):]
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:]
    return websocket.query_params.get("token")


async def authenticate_websocket(websocket: WebSocket) -> TokenClaims | None:
    """Validate the JWT on the upgrade. Closes the socket with 1008 + does NOT accept on failure.

    Returns the claims on success, or ``None`` after closing on failure so the
    caller can simply ``return``.
    """
    token = websocket_token(websocket)
    if not token:
        # Policy violation close code; mirrors the REST 401 (PRD §08).
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing token")
        return None
    try:
        return decode_token(token, expected_kind="ws")
    except ApiError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid token")
        return None
