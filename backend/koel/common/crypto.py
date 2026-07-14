"""Symmetric secret encryption for BYOK model-connection API keys.

Customer-supplied provider keys are stored encrypted at rest (never plaintext in
the DB, never on the inter-service wire). We use Fernet (AES-128-CBC + HMAC) with
a single master key from ``KOEL_SECRET_KEY`` (a urlsafe-base64 32-byte key, i.e.
``Fernet.generate_key()`` output).

Fail-closed: if the master key is unset/invalid, encryption/decryption raise
``SecretsUnavailable`` so the BYOK feature simply cannot be created or used —
existing env-configured providers are unaffected. In production the master key
comes from the secret manager (see ``docs/SECURITY.md``), not ``.env``.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class SecretsUnavailable(RuntimeError):
    """Raised when KOEL_SECRET_KEY is missing/invalid — BYOK secrets can't be used."""


_ENV_VAR = "KOEL_SECRET_KEY"


@lru_cache
def _fernet() -> Fernet:
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        raise SecretsUnavailable(
            f"{_ENV_VAR} is not set — BYOK model-connection secrets are disabled. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise SecretsUnavailable(f"{_ENV_VAR} is not a valid Fernet key") from exc


def secrets_available() -> bool:
    """True when a usable master key is configured (so the API can gate BYOK
    creation with a clear error instead of a 500)."""
    try:
        _fernet()
        return True
    except SecretsUnavailable:
        return False


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt a secret for at-rest storage. Raises SecretsUnavailable if no key."""
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(token: bytes | str) -> str:
    """Decrypt a stored secret. Raises SecretsUnavailable on a missing key or a
    token that doesn't verify (tampered / wrong key)."""
    raw = token.encode() if isinstance(token, str) else token
    try:
        return _fernet().decrypt(raw).decode()
    except InvalidToken as exc:
        raise SecretsUnavailable("secret does not verify (wrong key or tampered)") from exc


def mask_secret(plaintext: str) -> str:
    """Render a non-reversible hint for API responses — never the full value."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "…" + plaintext[-2:]
    return f"{plaintext[:3]}…{plaintext[-4:]}"
