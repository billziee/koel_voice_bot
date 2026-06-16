"""Integration tests for POST /v1/sessions (PRD §11 DPDP / TRAI / RBI + §05 seed).

Drives the real FastAPI app over httpx ASGITransport with in-memory fakes.
"""

from __future__ import annotations

from koel.crm.schemas import CustomerRecord
from koel.db.models import UseCase
from tests.conftest import seed_bot

CUSTOMER = CustomerRecord(
    id="cust_x",
    name="Rahul Sharma",
    phone="+91-90000-00001",
    language="hinglish",
    bank="HDFC Bank",
    use_case=UseCase.credit_card,
    dnd=False,
    attributes={
        "product": "Regalia Credit Card",
        "bureau_score": 780,
        "benefits": ["lounge access"],
        "internal_risk_note": "SHOULD_NOT_LEAK",  # not in the credit_card allowlist
    },
)


async def _start(client, headers, bot_id="bot_test", customer_id="cust_x"):
    return await client.post(
        "/v1/sessions", headers=headers, json={"bot_id": bot_id, "customer_id": customer_id}
    )


async def test_dpdp_minimisation_filters_non_allowlisted_fields(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db, config={"backchannels": {"hinglish": ["bilkul"]}})

    r = await _start(client, access_headers)
    assert r.status_code == 201, r.text
    ctx = r.json()["crm_context"]
    # Allowed fields present…
    assert ctx["product"] == "Regalia Credit Card"
    assert ctx["bureau_score"] == 780
    assert ctx["name"] == "Rahul Sharma"  # base identity field always kept
    # …non-allowlisted attribute stripped (DPDP data minimisation, §11).
    assert "internal_risk_note" not in ctx


async def test_recording_url_and_backchannels_seeded_into_state(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db, config={"backchannels": {"hinglish": ["bilkul", "sahi"]}})

    r = await _start(client, access_headers)
    assert r.status_code == 201, r.text
    state = runtime.store.data[r.json()["session_id"]]
    assert state.recording_url and state.recording_url.startswith("s3://koel-recordings/")
    assert state.backchannels == {"hinglish": ["bilkul", "sahi"]}


async def test_dnd_customer_refused_409(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER.model_copy(update={"dnd": True})
    await seed_bot(test_db)

    r = await _start(client, access_headers)
    assert r.status_code == 409
    assert r.json()["code"] == "dnd_registered"


async def test_inactive_bot_404(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db, active=False)

    r = await _start(client, access_headers)
    assert r.status_code == 404


async def test_rbi_calling_hours_refused_when_outside_window(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db)
    # 0 <= hour < 0 is never true → deterministically "outside hours".
    runtime.settings.enforce_calling_hours = True
    runtime.settings.calling_hours_start = 0
    runtime.settings.calling_hours_end = 0

    r = await _start(client, access_headers)
    assert r.status_code == 403
    assert r.json()["code"] == "outside_calling_hours"


async def test_rbi_calling_hours_allowed_within_window(app_client, access_headers, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db)
    runtime.settings.enforce_calling_hours = True
    runtime.settings.calling_hours_start = 0
    runtime.settings.calling_hours_end = 24  # always within

    r = await _start(client, access_headers)
    assert r.status_code == 201, r.text


async def test_unauthenticated_rejected_401(app_client, test_db):
    client, runtime = app_client
    runtime.clients.customer = CUSTOMER
    await seed_bot(test_db)

    r = await client.post("/v1/sessions", json={"bot_id": "bot_test", "customer_id": "cust_x"})
    assert r.status_code == 401
