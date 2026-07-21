"""create_app's production boot-time checks (docs/SECURITY.md checklist)."""

from __future__ import annotations

import types

from koel.common.app_factory import warn_if_byok_secret_unset


def _cfg(*, production: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(is_production=production)


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args, **kwargs) -> None:
        self.warnings.append(msg)


def test_prod_without_secret_key_warns(monkeypatch):
    monkeypatch.delenv("KOEL_SECRET_KEY", raising=False)
    from koel.common import crypto

    crypto._fernet.cache_clear()
    logger = _FakeLogger()
    assert warn_if_byok_secret_unset(_cfg(production=True), logger) is True
    assert len(logger.warnings) == 1
    assert "KOEL_SECRET_KEY" in logger.warnings[0]


def test_prod_with_valid_secret_key_is_silent(monkeypatch):
    from cryptography.fernet import Fernet

    from koel.common import crypto

    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    logger = _FakeLogger()
    assert warn_if_byok_secret_unset(_cfg(production=True), logger) is False
    assert logger.warnings == []
    crypto._fernet.cache_clear()  # don't leak the generated key into later tests


def test_non_prod_never_warns_even_when_unset(monkeypatch):
    monkeypatch.delenv("KOEL_SECRET_KEY", raising=False)
    from koel.common import crypto

    crypto._fernet.cache_clear()
    logger = _FakeLogger()
    assert warn_if_byok_secret_unset(_cfg(production=False), logger) is False
    assert logger.warnings == []
