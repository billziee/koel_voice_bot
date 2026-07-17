"""BYOK model-connection persistence + server-side resolution.

CRUD is used by the VB API (``api/connections.py``); ``resolve_connection`` is
used by NLP/TTS/STT services to turn a bot's stored connection id into decrypted
provider-build inputs. The plaintext key exists only transiently in
``ResolvedConnection`` (server memory) — it is encrypted at rest and never
returned by CRUD (responses carry ``api_key_set: bool`` only).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select

from koel.common.crypto import decrypt_secret, encrypt_secret
from koel.common.logging import get_logger
from koel.common.urlsafe import UnsafeUrlError, validate_base_url
from koel.db.models import ModelConnection
from koel.db.session import session_scope

logger = get_logger("koel.model_connections")

_KINDS = {"llm", "tts", "stt"}


@dataclass(frozen=True)
class ResolvedConnection:
    """Decrypted, ready-to-build connection (server-memory only — never serialized
    to a client or another service)."""

    id: str
    kind: str
    provider: str
    base_url: str
    model: str
    api_key: str
    params: dict = field(default_factory=dict)


def _to_public(row: ModelConnection) -> dict:
    """API-safe view — the key is reduced to a boolean, never exposed."""
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "kind": row.kind,
        "provider": row.provider,
        "base_url": row.base_url,
        "model": row.model,
        "params": row.params or {},
        "api_key_set": row.api_key_encrypted is not None,
        "active": row.active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def create_connection(
    *,
    org_id: str | None,
    name: str,
    kind: str,
    provider: str = "openai_compatible",
    base_url: str = "",
    api_key: str | None = None,
    model: str = "",
    params: dict | None = None,
) -> dict:
    """Create a connection. Validates the URL (SSRF) and encrypts the key at rest.
    Raises ValueError on a bad kind; UnsafeUrlError on a bad URL; SecretsUnavailable
    if a key is supplied but no master key is configured."""
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {sorted(_KINDS)}")
    if base_url:
        # validate_base_url does a blocking DNS lookup — off the event loop so a
        # slow resolver can't stall concurrent live voice calls on this worker.
        await asyncio.to_thread(validate_base_url, base_url)
    enc = encrypt_secret(api_key) if api_key else None
    async with session_scope() as db:
        row = ModelConnection(
            org_id=org_id, name=name, kind=kind, provider=provider,
            base_url=base_url, api_key_encrypted=enc, model=model, params=params or {},
        )
        db.add(row)
        await db.flush()
        return _to_public(row)


async def list_connections(org_id: str | None, kind: str | None = None) -> list[dict]:
    async with session_scope() as db:
        stmt = select(ModelConnection)
        if org_id is not None:
            stmt = stmt.where(ModelConnection.org_id == org_id)
        if kind is not None:
            stmt = stmt.where(ModelConnection.kind == kind)
        stmt = stmt.order_by(ModelConnection.created_at.desc())
        rows = (await db.execute(stmt)).scalars().all()
        return [_to_public(r) for r in rows]


async def get_connection(conn_id: str, org_id: str | None) -> dict | None:
    async with session_scope() as db:
        row = await db.get(ModelConnection, conn_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        return _to_public(row)


async def update_connection(conn_id: str, org_id: str | None, **fields: object) -> dict | None:
    """Update mutable fields. A supplied ``api_key`` is re-encrypted; a supplied
    ``base_url`` is re-validated. Unknown fields are ignored."""
    async with session_scope() as db:
        row = await db.get(ModelConnection, conn_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        if "base_url" in fields and fields["base_url"]:
            await asyncio.to_thread(validate_base_url, str(fields["base_url"]))
        for attr in ("name", "provider", "base_url", "model"):
            if attr in fields and fields[attr] is not None:
                setattr(row, attr, str(fields[attr]))
        if "params" in fields and isinstance(fields["params"], dict):
            row.params = fields["params"]
        if fields.get("api_key"):
            row.api_key_encrypted = encrypt_secret(str(fields["api_key"]))
        await db.flush()
        return _to_public(row)


async def deactivate_connection(conn_id: str, org_id: str | None) -> dict | None:
    async with session_scope() as db:
        row = await db.get(ModelConnection, conn_id)
        if row is None or (org_id is not None and row.org_id != org_id):
            return None
        row.active = False
        await db.flush()
        return _to_public(row)


async def resolve_connection(conn_id: str | None, org_id: str | None = None) -> ResolvedConnection | None:
    """Server-side resolution for a service (NLP/TTS/STT): fetch the row, decrypt
    the key, return build inputs. Returns None when absent, inactive, cross-tenant,
    or the key can't be decrypted (fail closed → caller uses its default provider).
    ``conn_id`` came from the bot's own config, already org-authorized at config
    time; ``org_id`` adds defense-in-depth when the caller has it."""
    if not conn_id:
        return None
    async with session_scope() as db:
        row = await db.get(ModelConnection, conn_id)
        if row is None or not row.active or (org_id is not None and row.org_id != org_id):
            return None
        # SSRF re-validation ON THE LIVE PATH (not just at create): a stored URL
        # can be DNS-rebound to a private/metadata address after it first passed.
        # Re-check every resolve so the service never builds an outbound client
        # (carrying the decrypted key) against an internal target. Off the event
        # loop; the OS resolver caches so the repeat cost is negligible. Fail
        # closed → caller uses its default provider.
        if row.base_url:
            try:
                await asyncio.to_thread(validate_base_url, row.base_url)
            except UnsafeUrlError:
                logger.warning("model_connection_unsafe_base_url id=%s", conn_id)
                return None
        api_key = ""
        if row.api_key_encrypted is not None:
            try:
                api_key = decrypt_secret(row.api_key_encrypted)
            except Exception:  # noqa: BLE001 — never leak/raise; degrade to default
                logger.warning("model_connection_decrypt_failed id=%s", conn_id)
                return None
        return ResolvedConnection(
            id=row.id, kind=row.kind, provider=row.provider, base_url=row.base_url,
            model=row.model, api_key=api_key, params=dict(row.params or {}),
        )
