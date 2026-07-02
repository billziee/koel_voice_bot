"""CRM interaction persistence + idempotent retries (AD)."""

from __future__ import annotations

from sqlalchemy import func, select

from koel.common.auth import TokenClaims
from koel.crm.main import log_interaction
from koel.crm.schemas import InteractionLog
from koel.db.models import InteractionLogRow

_CLAIMS = TokenClaims(sub="svc", kind="access", role="service", exp=9_999_999_999, iat=0, jti="j")


async def _count(maker) -> int:
    async with maker() as s:
        return (await s.execute(select(func.count()).select_from(InteractionLogRow))).scalar_one()


async def test_interaction_is_persisted(test_db):
    payload = InteractionLog(customer_id="c1", session_id="s1", event="promise_to_pay")
    res = await log_interaction("c1", payload, _CLAIMS)
    assert res["status"] == "logged"
    assert await _count(test_db) == 1


async def test_same_idempotency_key_writes_once(test_db):
    payload = InteractionLog(
        customer_id="c1", session_id="s1", event="opt_out", idempotency_key="s1:opt_out"
    )
    first = await log_interaction("c1", payload, _CLAIMS)
    second = await log_interaction("c1", payload, _CLAIMS)  # retry
    assert first["status"] == "logged"
    assert second["status"] == "duplicate"
    assert await _count(test_db) == 1


async def test_different_keys_write_separate_rows(test_db):
    for i in range(3):
        await log_interaction(
            "c1",
            InteractionLog(customer_id="c1", session_id="s1", event="turn", idempotency_key=f"k{i}"),
            _CLAIMS,
        )
    assert await _count(test_db) == 3
