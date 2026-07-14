"""BYOK secret encryption (koel.common.crypto)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from koel.common import crypto


@pytest.fixture
def _with_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001


def test_round_trips_a_secret(_with_key):
    token = crypto.encrypt_secret("sk_live_supersecret")
    assert token != b"sk_live_supersecret"
    assert crypto.decrypt_secret(token) == "sk_live_supersecret"
    assert crypto.secrets_available() is True


def test_disabled_without_master_key(monkeypatch):
    monkeypatch.delenv("KOEL_SECRET_KEY", raising=False)
    crypto._fernet.cache_clear()  # noqa: SLF001
    assert crypto.secrets_available() is False
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.encrypt_secret("x")
    crypto._fernet.cache_clear()  # noqa: SLF001


def test_decrypt_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    token = crypto.encrypt_secret("secret")
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.decrypt_secret(token)
    crypto._fernet.cache_clear()  # noqa: SLF001


def test_mask_never_reveals_full_value():
    assert crypto.mask_secret("sk_abcdefghij") == "sk_…ghij"
    assert crypto.mask_secret("short") == "…rt"
    assert crypto.mask_secret("") == ""
