"""POST /v1/customers/{id}/call-summary — appends the LLM summarizer's output
onto the customer record, deduped on session_id (AD idempotency convention)."""

from __future__ import annotations

from koel.common.auth import TokenClaims
from koel.crm.main import add_call_summary, get_customer
from koel.crm.schemas import CallSummaryRequest
from koel.db.models import CrmCustomer, UseCase

_CLAIMS = TokenClaims(sub="svc", kind="access", role="service", exp=9_999_999_999, iat=0, jti="j")


async def _seed_customer(test_db, customer_id: str = "c1") -> None:
    async with test_db() as s:
        s.add(CrmCustomer(
            id=customer_id, name="Meera Iyer", phone="+911234567890",
            language="hi", bank="Devotica Life", use_case=UseCase.insurance,
        ))
        await s.commit()


async def test_call_summary_appends_to_attributes(test_db):
    await _seed_customer(test_db)
    payload = CallSummaryRequest(
        session_id="s1", interest_shown=True, objection="", sentiment="positive",
        next_action="Follow up with email",
    )
    res = await add_call_summary("c1", payload, _CLAIMS)
    assert res["status"] == "logged"

    record = await get_customer("c1", _CLAIMS)
    notes = record.attributes["call_notes"]
    assert len(notes) == 1
    assert notes[0]["session_id"] == "s1"
    assert notes[0]["interest_shown"] is True
    assert notes[0]["next_action"] == "Follow up with email"


async def test_call_summary_dedupes_on_session_id(test_db):
    await _seed_customer(test_db)
    payload = CallSummaryRequest(session_id="s1", interest_shown=False)
    first = await add_call_summary("c1", payload, _CLAIMS)
    second = await add_call_summary("c1", payload, _CLAIMS)  # retry
    assert first["status"] == "logged"
    assert second["status"] == "duplicate"

    record = await get_customer("c1", _CLAIMS)
    assert len(record.attributes["call_notes"]) == 1


async def test_call_summary_accumulates_across_multiple_calls(test_db):
    await _seed_customer(test_db)
    await add_call_summary("c1", CallSummaryRequest(session_id="s1", interest_shown=True), _CLAIMS)
    await add_call_summary("c1", CallSummaryRequest(session_id="s2", interest_shown=False), _CLAIMS)

    record = await get_customer("c1", _CLAIMS)
    sessions = {n["session_id"] for n in record.attributes["call_notes"]}
    assert sessions == {"s1", "s2"}


async def test_call_summary_404s_for_unknown_customer(test_db):
    from koel.common.errors import ApiError

    payload = CallSummaryRequest(session_id="s1", interest_shown=True)
    try:
        await add_call_summary("does-not-exist", payload, _CLAIMS)
        raise AssertionError("expected ApiError")
    except ApiError as e:
        assert e.status_code == 404
