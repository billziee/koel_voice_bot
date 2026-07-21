"""Real human-escalation handoff (3.1 Phase A): hold state, queue, live bridge."""

from __future__ import annotations

from koel.common.auth import issue_token
from koel.common.schemas import Language
from koel.db.models import EscalationStatus
from koel.voicebot.escalation import EscalationHub
from koel.voicebot.persistence import (
    abandon_escalation_by_session,
    claim_escalation,
    close_escalation,
    create_escalation,
    get_escalation,
    list_pending_escalations,
)
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.ws.handler import CallSession
from koel.voicebot.ws.protocol import ClientMessage, ClientMessageType, ServerMessageType
from tests.conftest import FakeClients, FakeStore, VoiceBotSettings


def _state(session_id: str = "s1", org_id: str | None = "org1") -> SessionState:
    st = SessionState(session_id=session_id, bot_id="bot_x", customer_id="cust_x", use_case="collections")
    st.org_id = org_id
    st.language = Language.hinglish
    st.crm_context = {"name": "Rahul", "bank": "HDFC"}
    st.add_turn(Turn(index=0, speaker="bot", text="Namaste"))
    st.add_turn(Turn(index=1, speaker="customer", text="Kisi bande se baat karao"))
    return st


# --- EscalationHub (in-process live bridge) ---------------------------------

async def test_hub_delivers_and_resolves_when_registered():
    hub = EscalationHub()
    delivered: list[str] = []
    resolved: list[bool] = []

    async def deliver(text: str) -> None:
        delivered.append(text)

    async def resolve() -> None:
        resolved.append(True)

    hub.register("s1", deliver=deliver, resolve=resolve)
    assert hub.is_live("s1") is True
    assert await hub.deliver("s1", "maine aapka payment dekh liya") is True
    assert delivered == ["maine aapka payment dekh liya"]
    assert await hub.resolve("s1") is True
    assert resolved == [True]


async def test_hub_returns_false_when_not_registered():
    hub = EscalationHub()
    assert hub.is_live("nope") is False
    assert await hub.deliver("nope", "hi") is False
    assert await hub.resolve("nope") is False


async def test_hub_unregister_stops_delivery():
    hub = EscalationHub()

    async def _noop(_text: str = "") -> None:
        return None

    hub.register("s1", deliver=_noop, resolve=_noop)
    hub.unregister("s1")
    assert await hub.deliver("s1", "x") is False


# --- Orchestrator: _escalate holds (does NOT end); deliver speaks -----------

def _orch() -> Orchestrator:
    return Orchestrator(settings=VoiceBotSettings(), clients=FakeClients(), filler_cache=FillerCache())


async def test_escalate_parks_in_waiting_agent_and_does_not_end():
    orch = _orch()
    state = _state()
    frames = []

    async def send(msg):  # noqa: ANN001
        frames.append(msg)

    await orch._escalate(state, send, reason="request_human")

    assert state.status == CallStatus.waiting_agent
    assert state.escalated is True
    assert state.escalation_reason == "request_human"
    assert state.outcome == "escalated"
    # The full-context handoff frame is emitted (PRD §02)...
    handoff = [f for f in frames if f.type == ServerMessageType.call_state
               and f.data.get("status") == "escalating"]
    assert handoff, "expected an escalating call_state frame with context"
    assert handoff[0].data["transcript"] and "crm_context" in handoff[0].data
    # ...a hold line is spoken...
    assert any(f.type == ServerMessageType.audio_chunk for f in frames)
    # ...and the call is NOT ended.
    assert not any(f.type == ServerMessageType.call_ended for f in frames)


def _callsession(state: SessionState, clients: FakeClients) -> CallSession:
    settings = VoiceBotSettings()
    return CallSession(
        websocket=None,
        state=state,
        settings=settings,
        store=FakeStore(),
        orchestrator=Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache()),
        bargein=BargeInClassifier(backchannel_max_speech_ms=settings.backchannel_max_speech_ms),
        clients=clients,
        resumed=False,
    )


async def test_caller_audio_during_hold_is_ignored_by_bot():
    # While waiting_agent, the human operator owns the call — caller audio must
    # NOT run the STT/NLP turn loop. FakeClients.transcribe() raises, so if the
    # guard were missing this would blow up instead of quietly returning.
    clients = FakeClients()
    state = _state()
    state.status = CallStatus.waiting_agent
    sess = _callsession(state, clients)
    msg = ClientMessage(
        type=ClientMessageType.audio_chunk, audio_b64="AAAA", sample_rate=16000, is_final=True
    )
    await sess._handle_audio(msg)  # returns without touching transcribe → bot stays silent


async def test_deliver_agent_turn_speaks_to_caller():
    orch = _orch()
    state = _state()
    frames = []

    async def send(msg):  # noqa: ANN001
        frames.append(msg)

    await orch.deliver_agent_turn(state, send, text="Aapka refund process ho gaya hai")
    assert any(f.type == ServerMessageType.audio_chunk for f in frames)
    assert any(
        f.type == ServerMessageType.transcript
        and "refund" in (f.data.get("text") or "")
        for f in frames
    )


# --- Persistence: queue lifecycle + tenant scoping --------------------------

async def test_create_list_claim_resolve_lifecycle(test_db):
    eid = await create_escalation(_state(session_id="sX", org_id="org1"), reason="request_human")

    pending = await list_pending_escalations("org1")
    assert [e["id"] for e in pending] == [eid]
    assert pending[0]["reason"] == "request_human"
    assert pending[0]["transcript"] and pending[0]["crm_context"]["name"] == "Rahul"

    claimed = await claim_escalation(eid, "org1", "op_1")
    assert claimed is not None and claimed["status"] == EscalationStatus.claimed.value
    assert claimed["operator_sub"] == "op_1"
    # Claimed rows leave the pending queue; a second claim is rejected.
    assert await list_pending_escalations("org1") == []
    assert await claim_escalation(eid, "org1", "op_2") is None

    resolved = await close_escalation(eid, "org1", EscalationStatus.resolved)
    assert resolved is not None and resolved["status"] == EscalationStatus.resolved.value


async def test_persistence_is_tenant_scoped(test_db):
    eid = await create_escalation(_state(session_id="sY", org_id="orgA"), reason="x")
    # Another tenant can't see or claim it.
    assert await list_pending_escalations("orgB") == []
    assert await get_escalation(eid, "orgB") is None
    assert await claim_escalation(eid, "orgB", "op") is None
    # Its own org can.
    assert await get_escalation(eid, "orgA") is not None


async def test_abandon_by_session_marks_open_handoffs(test_db):
    eid = await create_escalation(_state(session_id="sZ", org_id="org1"), reason="x")
    await abandon_escalation_by_session("sZ")
    row = await get_escalation(eid, "org1")
    assert row["status"] == EscalationStatus.abandoned.value
    # Abandoning is idempotent and never resurrects a resolved handoff.
    eid2 = await create_escalation(_state(session_id="sW", org_id="org1"), reason="x")
    await close_escalation(eid2, "org1", EscalationStatus.resolved)
    await abandon_escalation_by_session("sW")
    assert (await get_escalation(eid2, "org1"))["status"] == EscalationStatus.resolved.value


# --- Operator API (org-unscoped demo token sees all) ------------------------

def _op_headers(org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op_1', kind='access', role='operator', org_id=org_id)}"}


async def test_operator_queue_claim_reply_resolve(app_client, test_db):
    client, runtime = app_client
    eid = await create_escalation(_state(session_id="live1", org_id=None), reason="request_human")

    # Queue lists the pending handoff.
    r = await client.get("/v1/operator/queue", headers=_op_headers())
    assert r.status_code == 200
    assert [e["id"] for e in r.json()] == [eid]

    # Reply before claiming is rejected.
    r = await client.post(
        f"/v1/operator/escalations/{eid}/reply", headers=_op_headers(), json={"text": "hi"}
    )
    assert r.status_code == 409

    # Claim it.
    r = await client.post(f"/v1/operator/escalations/{eid}/claim", headers=_op_headers())
    assert r.status_code == 200 and r.json()["status"] == "claimed"
    # Double-claim → 409.
    r2 = await client.post(f"/v1/operator/escalations/{eid}/claim", headers=_op_headers())
    assert r2.status_code == 409

    # Reply with no live caller registered → 410 Gone.
    r = await client.post(
        f"/v1/operator/escalations/{eid}/reply", headers=_op_headers(), json={"text": "one sec"}
    )
    assert r.status_code == 410

    # Register a live caller on the hub, then a reply is delivered.
    spoken: list[str] = []
    resolved: list[bool] = []

    async def deliver(text: str) -> None:
        spoken.append(text)

    async def resolve() -> None:
        resolved.append(True)

    runtime.escalations.register("live1", deliver=deliver, resolve=resolve)
    r = await client.post(
        f"/v1/operator/escalations/{eid}/reply",
        headers=_op_headers(),
        json={"text": "Aapka issue solve kar diya"},
    )
    assert r.status_code == 200
    assert spoken == ["Aapka issue solve kar diya"]

    # Resolve marks the row resolved AND ends the caller's call via the hub.
    r = await client.post(f"/v1/operator/escalations/{eid}/resolve", headers=_op_headers())
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert resolved == [True]


async def test_reply_and_resolve_reject_a_non_claiming_operator(app_client, test_db):
    # Regression: org + claimed status is not enough — only the operator who
    # CLAIMED the handoff may reply/resolve. A second operator in the SAME org must
    # get 403, otherwise they could talk over (or end) another operator's live call.
    client, _ = app_client
    eid = await create_escalation(_state(session_id="live2", org_id=None), reason="request_human")

    op2 = {"Authorization": f"Bearer {issue_token('op_2', kind='access', role='operator')}"}

    # op_1 claims it.
    r = await client.post(f"/v1/operator/escalations/{eid}/claim", headers=_op_headers())
    assert r.status_code == 200

    # op_2 (same org) may not reply — the ownership check short-circuits before the
    # message is ever delivered to the caller.
    r = await client.post(
        f"/v1/operator/escalations/{eid}/reply", headers=op2, json={"text": "butting in"}
    )
    assert r.status_code == 403 and r.json()["code"] == "not_claim_owner"
    # ...nor resolve.
    r = await client.post(f"/v1/operator/escalations/{eid}/resolve", headers=op2)
    assert r.status_code == 403 and r.json()["code"] == "not_claim_owner"


async def test_operator_endpoints_require_auth(app_client, test_db):
    client, _ = app_client
    assert (await client.get("/v1/operator/queue")).status_code == 401


async def test_claim_missing_escalation_is_404(app_client, test_db):
    client, _ = app_client
    r = await client.post("/v1/operator/escalations/nope/claim", headers=_op_headers())
    assert r.status_code == 404
