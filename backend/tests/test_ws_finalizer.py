"""Integration test for the dropped-call finalizer (PRD §05 no-reconnect branch).

Drives CallSession._await_grace_then_finalize directly with a 0s grace window,
the in-memory store, and fake clients — asserting the call is recorded as
call_dropped, the CRM is flagged for callback, and a reconnect suppresses it.
"""

from __future__ import annotations

from sqlalchemy import select

from koel.db.models import CallLog, CallOutcome
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.ws.handler import CallSession
from tests.conftest import FakeClients, FakeStore


def _reconnecting_state(session_id="drop_1") -> SessionState:
    s = SessionState(session_id=session_id, bot_id="bot_test", customer_id="cust_x", use_case="credit_card")
    s.status = CallStatus.reconnecting
    s.add_turn(Turn(index=0, speaker="bot", text="Namaste"))
    s.add_turn(Turn(index=1, speaker="customer", text="Haan boliye"))
    return s


def _session(state, store, clients) -> CallSession:
    return CallSession(
        websocket=None,
        state=state,
        settings=VoiceBotSettings(session_grace_ttl_seconds=0),
        store=store,
        orchestrator=None,
        bargein=None,
        clients=clients,
        resumed=False,
    )


async def test_grace_expiry_finalizes_as_call_dropped(test_db):
    state = _reconnecting_state()
    store = FakeStore()
    await store.save(state)
    clients = FakeClients()
    call = _session(state, store, clients)

    await call._await_grace_then_finalize()  # noqa: SLF001 — exercising the finalizer

    # Outcome flipped, CRM flagged for callback, session flushed from the store.
    assert state.outcome == "call_dropped"
    assert ("cust_x", "callback", "call dropped mid-conversation") in clients.flags
    assert "drop_1" in store.deleted
    # Partial transcript persisted to the DB.
    async with test_db() as db:
        row = (await db.execute(select(CallLog).where(CallLog.session_id == "drop_1"))).scalar_one()
    assert row.outcome == CallOutcome.call_dropped


async def test_reconnect_suppresses_finalizer(test_db):
    # The store now reports the session resumed (status moved off reconnecting).
    state = _reconnecting_state(session_id="drop_2")
    store = FakeStore()
    resumed = state.model_copy(update={"status": CallStatus.listening})
    await store.save(resumed)
    clients = FakeClients()
    call = _session(state, store, clients)

    await call._await_grace_then_finalize()  # noqa: SLF001

    # No drop recorded: no CRM flag, nothing deleted, nothing persisted.
    assert clients.flags == []
    assert store.deleted == []
    async with test_db() as db:
        rows = (await db.execute(select(CallLog).where(CallLog.session_id == "drop_2"))).scalars().all()
    assert rows == []
