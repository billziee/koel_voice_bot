"""BYOK model-connection persistence + resolution (koel.common.model_connections)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from koel.common import crypto
from koel.common.model_connections import (
    create_connection,
    deactivate_connection,
    get_connection,
    list_connections,
    resolve_connection,
    update_connection,
)
from koel.common.urlsafe import UnsafeUrlError


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setenv("KOEL_SECRET_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()  # noqa: SLF001
    yield
    crypto._fernet.cache_clear()  # noqa: SLF001


async def test_create_never_exposes_the_key_but_resolve_decrypts_it(test_db):
    pub = await create_connection(
        org_id="orgA", name="my-llm", kind="llm", provider="openai_compatible",
        base_url="https://api.groq.com/openai/v1", api_key="sk_secret_123", model="llama-3.3-70b",
        params={"temperature": 0.4},
    )
    # Public view carries only a boolean — never the key.
    assert pub["api_key_set"] is True
    assert "api_key" not in pub and "sk_secret_123" not in str(pub)

    # Server-side resolution decrypts the real key for provider build.
    resolved = await resolve_connection(pub["id"])
    assert resolved is not None
    assert resolved.api_key == "sk_secret_123"
    assert resolved.model == "llama-3.3-70b"
    assert resolved.params == {"temperature": 0.4}


async def test_org_scoping_blocks_cross_tenant_read(test_db):
    pub = await create_connection(org_id="orgA", name="a", kind="llm", base_url="", api_key="k")
    assert await get_connection(pub["id"], org_id="orgB") is None  # other tenant
    assert await get_connection(pub["id"], org_id="orgA") is not None
    # list is org-scoped
    assert {c["id"] for c in await list_connections("orgB")} == set()
    assert pub["id"] in {c["id"] for c in await list_connections("orgA")}


async def test_update_reencrypts_key_and_revalidates_url(test_db):
    pub = await create_connection(org_id="orgA", name="a", kind="tts", api_key="old")
    await update_connection(pub["id"], "orgA", api_key="new_key", model="aura-2")
    resolved = await resolve_connection(pub["id"])
    assert resolved is not None and resolved.api_key == "new_key" and resolved.model == "aura-2"
    with pytest.raises(UnsafeUrlError):
        await update_connection(pub["id"], "orgA", base_url="http://169.254.169.254")


async def test_deactivate_makes_resolve_fail_closed(test_db):
    pub = await create_connection(org_id="orgA", name="a", kind="stt", api_key="k")
    await deactivate_connection(pub["id"], "orgA")
    assert await resolve_connection(pub["id"]) is None  # inactive → default provider


async def test_bad_kind_and_bad_url_rejected(test_db):
    with pytest.raises(ValueError):
        await create_connection(org_id="orgA", name="x", kind="image")
    with pytest.raises(UnsafeUrlError):
        await create_connection(org_id="orgA", name="x", kind="llm", base_url="http://localhost")


async def test_resolve_none_for_missing_or_empty(test_db):
    assert await resolve_connection(None) is None
    assert await resolve_connection("does-not-exist") is None


async def test_resolve_re_validates_base_url_ssrf_on_the_live_path(test_db):
    # Regression: a stored URL can be DNS-rebound (or inserted) to an internal /
    # metadata address after create-time validation. resolve_connection MUST
    # re-validate on the live path and fail closed, so the service never builds an
    # outbound client (carrying the decrypted key) against a private target.
    from koel.db.models import ModelConnection
    from koel.db.session import session_scope

    async with session_scope() as db:  # insert directly, bypassing create()'s guard
        db.add(ModelConnection(
            id="c_rebound", org_id="orgA", name="rebound", kind="llm",
            provider="openai_compatible", base_url="http://169.254.169.254/v1",
            api_key_encrypted=crypto.encrypt_secret("k"), model="m",
        ))
    assert await resolve_connection("c_rebound") is None
