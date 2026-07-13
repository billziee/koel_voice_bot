"""BYOK model-connection CRUD API (Phase 2b).

A per-org (tenant-scoped) surface over ``koel.common.model_connections`` for
managing customer-supplied provider connections (LLM / TTS / STT). Every route is
scoped by the caller's ``org_id`` — a tenant sees only its own connections; an
unscoped platform principal (``org_id is None``) sees the shared/platform set,
consistent with every other scoped router.

The provider API key is WRITE-ONLY: it is accepted on create/update, encrypted at
rest, and NEVER returned or logged. Responses carry ``api_key_set: bool`` only.
The persistence + resolution layer already does the encryption, SSRF URL
validation, and org-scoping (Phase 1); this router maps its exceptions onto the
shared :class:`ApiError` envelope and adds the ``/test`` connectivity probe.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from koel.common.auth import CurrentUser
from koel.common.crypto import SecretsUnavailable, secrets_available
from koel.common.errors import ApiError
from koel.common.logging import get_logger
from koel.common.model_connections import (
    ResolvedConnection,
    create_connection,
    deactivate_connection,
    get_connection,
    list_connections,
    resolve_connection,
    update_connection,
)
from koel.common.ratelimit import rate_limit
from koel.common.urlsafe import UnsafeUrlError, validate_base_url

logger = get_logger("koel.voicebot.connections")
router = APIRouter(prefix="/v1/model-connections", tags=["model-connections"])

# The /test probe fans out to a customer endpoint — cap it per IP so it can't be
# turned into an SSRF/port-scan amplifier or a way to burn a provider's quota.
_test_limit = rate_limit(max_requests=30, window_s=60, name="connection_test")

# Short outbound budget for the liveness probe: a slow/dead provider must fail
# fast, not hold the request open (PRD latency discipline).
_PROBE_TIMEOUT_S = 8.0

_SECRETS_DISABLED_MSG = "KOEL_SECRET_KEY not configured; BYOK keys unavailable"


# --- Schemas -----------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """Create body. ``api_key`` is write-only — accepted here, never echoed back."""

    name: str = Field(min_length=1, max_length=120)
    # Free-form so a bad kind is surfaced by the model layer (ValueError → 422)
    # with its authoritative allowed-set message, rather than duplicated here.
    kind: str = Field(min_length=1, max_length=8)
    provider: str = Field(default="openai_compatible", max_length=32)
    base_url: str = Field(default="", max_length=256)
    api_key: str | None = Field(default=None, max_length=512)
    model: str = Field(default="", max_length=128)
    params: dict = Field(default_factory=dict)


class ConnectionUpdate(BaseModel):
    """Partial update — only the fields actually sent are applied."""

    name: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, max_length=32)
    base_url: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    params: dict | None = None


class ConnectionOut(BaseModel):
    """API-safe view. Deliberately has NO ``api_key`` field — only ``api_key_set``."""

    id: str
    org_id: str | None = None
    name: str
    kind: str
    provider: str
    base_url: str
    model: str
    params: dict
    api_key_set: bool
    active: bool
    created_at: str | None = None


class ConnectionTestResult(BaseModel):
    ok: bool
    detail: str
    latency_ms: int = 0


def _to_out(public: dict) -> ConnectionOut:
    """Project the persistence layer's public dict onto the response model.

    ``ConnectionOut`` has no ``api_key`` field, so even a future leak in the
    public dict cannot reach the wire through this router.
    """
    return ConnectionOut.model_validate(public)


@contextmanager
def _map_connection_errors() -> Iterator[None]:
    """Translate the persistence layer's exceptions onto the shared envelope.

    ``UnsafeUrlError`` subclasses ``ValueError`` so it MUST be caught first.
    """
    try:
        yield
    except UnsafeUrlError as exc:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="unsafe_base_url",
            message=str(exc),
        ) from exc
    except ValueError as exc:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_connection",
            message=str(exc),
        ) from exc
    except SecretsUnavailable as exc:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="secrets_disabled",
            message=_SECRETS_DISABLED_MSG,
        ) from exc


# --- Routes ------------------------------------------------------------------


@router.get("", response_model=list[ConnectionOut])
async def list_model_connections(
    user: CurrentUser,
    kind: str | None = None,
) -> list[ConnectionOut]:
    """All connections visible to the caller's org, newest first; optional ``?kind=``."""
    rows = await list_connections(user.org_id, kind)
    return [_to_out(r) for r in rows]


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def create_model_connection(payload: ConnectionCreate, user: CurrentUser) -> ConnectionOut:
    """Create a connection for the caller's org. The key (if any) is encrypted at rest."""
    # Fail early with a clear 400 when a key is supplied but the master key is
    # absent — otherwise the encrypt call would raise SecretsUnavailable anyway,
    # but this gives the crisp, documented error before touching the DB.
    if payload.api_key and not secrets_available():
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="secrets_disabled",
            message=_SECRETS_DISABLED_MSG,
        )
    with _map_connection_errors():
        public = await create_connection(
            org_id=user.org_id,
            name=payload.name,
            kind=payload.kind,
            provider=payload.provider,
            base_url=payload.base_url,
            api_key=payload.api_key,
            model=payload.model,
            params=payload.params,
        )
    logger.info(
        "model_connection_created",
        extra={"extra_fields": {"id": public["id"], "kind": public["kind"], "org_id": user.org_id}},
    )
    return _to_out(public)


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_model_connection(connection_id: str, user: CurrentUser) -> ConnectionOut:
    """A single connection by id. 404 if absent or owned by another org."""
    public = await get_connection(connection_id, user.org_id)
    if public is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="connection_not_found",
            message=f"No model connection with id {connection_id}.",
        )
    return _to_out(public)


@router.patch("/{connection_id}", response_model=ConnectionOut)
async def update_model_connection(
    connection_id: str, payload: ConnectionUpdate, user: CurrentUser
) -> ConnectionOut:
    """Partially update a connection. A supplied key is re-encrypted; a supplied
    base_url is re-validated. 404 if absent or owned by another org."""
    if payload.api_key and not secrets_available():
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="secrets_disabled",
            message=_SECRETS_DISABLED_MSG,
        )
    with _map_connection_errors():
        public = await update_connection(
            connection_id, user.org_id, **payload.model_dump(exclude_unset=True)
        )
    if public is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="connection_not_found",
            message=f"No model connection with id {connection_id}.",
        )
    logger.info(
        "model_connection_updated",
        extra={"extra_fields": {"id": connection_id, "org_id": user.org_id}},
    )
    return _to_out(public)


@router.post("/{connection_id}/deactivate", response_model=ConnectionOut)
async def deactivate_model_connection(connection_id: str, user: CurrentUser) -> ConnectionOut:
    """Soft-disable a connection (``active=False``). 404 if absent or cross-tenant.
    Idempotent — deactivating an already-inactive connection is a no-op."""
    public = await deactivate_connection(connection_id, user.org_id)
    if public is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="connection_not_found",
            message=f"No model connection with id {connection_id}.",
        )
    logger.info(
        "model_connection_deactivated",
        extra={"extra_fields": {"id": connection_id, "org_id": user.org_id}},
    )
    return _to_out(public)


@router.post(
    "/{connection_id}/test",
    response_model=ConnectionTestResult,
    dependencies=[Depends(_test_limit)],
)
async def test_model_connection(connection_id: str, user: CurrentUser) -> ConnectionTestResult:
    """Verify a connection is usable.

    Resolving it server-side already proves the key decrypts and the connection is
    active. For ``llm`` we go one step further with a real 1-token probe against
    ``/chat/completions``; for ``tts``/``stt`` the live audio path is exercised at
    call time, so a successful resolve is the validation. The api_key is used only
    as the outbound ``Authorization`` header — it is never echoed or logged.
    """
    resolved = await resolve_connection(connection_id, user.org_id)
    if resolved is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="connection_not_found",
            message=f"No active model connection with id {connection_id}.",
        )

    # Defense-in-depth: re-validate the stored endpoint right before using it
    # (DNS-rebind resistant — a host that resolved public at create time might
    # resolve private now). Only meaningful when a base_url is configured.
    if resolved.base_url:
        with _map_connection_errors():
            validate_base_url(resolved.base_url)

    if resolved.kind == "llm":
        return await _probe_llm(resolved)

    return ConnectionTestResult(
        ok=True,
        detail="configuration valid; live audio validated at call time",
    )


async def _probe_llm(resolved: ResolvedConnection) -> ConnectionTestResult:
    """One real, cheap 1-token completion against the resolved LLM endpoint.

    Returns ``ok`` on any 2xx; on a non-2xx or a transport error returns
    ``ok=false`` with a concise, key-free detail. Never raises — a failed probe is
    a normal result, not a server error.
    """
    if not resolved.base_url:
        return ConnectionTestResult(ok=False, detail="No base_url configured to probe.")

    url = f"{resolved.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {resolved.api_key}"} if resolved.api_key else {}
    body = {
        "model": resolved.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001 — any transport failure → ok=false, never 500
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.warning("model_connection_probe_failed id=%s kind=llm", resolved.id)
        return ConnectionTestResult(ok=False, detail=_safe_detail(exc), latency_ms=latency_ms)

    latency_ms = int((time.monotonic() - start) * 1000)
    ok = 200 <= resp.status_code < 300
    detail = (
        f"provider responded HTTP {resp.status_code}"
        if ok
        else f"provider returned HTTP {resp.status_code}"
    )
    return ConnectionTestResult(ok=ok, detail=detail, latency_ms=latency_ms)


def _safe_detail(exc: Exception) -> str:
    """A concise error hint that never carries the api_key.

    The key only ever lives in the request header, so it is not present in httpx
    exception strings; we still keep the detail to the exception type + message.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name
