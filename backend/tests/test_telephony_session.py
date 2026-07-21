"""TelephonyCallSession — the phone-transport analogue of ws/handler.CallSession.

Exercises everything the telephony/web parity audit found missing: barge-in
Gate 1 for the buffered ingest, smart-turn hold + merge, speculative RAG,
human-handoff registration + gating, the no-input watchdog, and end-of-call
persistence (a telephony call is now actually written to Postgres and cleaned
up from Redis, which it never was before).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from koel.common.schemas import Language, Tone
from koel.db.models import CallLog
from koel.nlp.schemas import Intent, NlpResponse, RetrievedChunk
from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import get_voicebot_settings
from koel.voicebot.escalation import EscalationHub
from koel.voicebot.monitor import LiveMonitor
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.endpointing import merge_transcripts
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.telephony.session import TelephonyCallSession
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType
from tests.conftest import FakeClients, FakeStore


class ScriptedClients(FakeClients):
    def __init__(self, *, nlp: NlpResponse, stt_text: str = "haan boliye", retrieve_chunks=None) -> None:
        super().__init__()
        self._nlp = nlp
        self._stt_text = stt_text
        self._retrieve_chunks = retrieve_chunks
        self.retrieve_calls: list = []

    async def transcribe(self, request):  # noqa: ANN001
        return TranscriptResult(
            session_id=request.session_id, text=self._stt_text, is_final=request.is_final,
            language_id=Language.hinglish, language_confidence=0.95,
            speech_duration_ms=800, word_confidence=0.9,
        )

    async def respond(self, ctx):  # noqa: ANN001
        return self._nlp.model_copy(update={"session_id": ctx.session_id})

    async def retrieve(self, ctx):  # noqa: ANN001 — only present when tests wire O-5
        self.retrieve_calls.append(ctx)
        return self._retrieve_chunks if self._retrieve_chunks is not None else []


class SmartTurnStub:
    """Returns each queued verdict in order, then True (complete) forever."""

    def __init__(self, verdicts: list[bool]) -> None:
        self._verdicts = list(verdicts)

    async def is_complete(self, pcm16_b64: str) -> bool | None:  # noqa: ARG002
        if self._verdicts:
            return self._verdicts.pop(0)
        return True


class FakeBridge:
    """Records everything TelephonyCallSession does through the bridge seam,
    and — like the real bridge — invokes the session's on_outbound hook on
    every send() so the full wiring (send → bookkeeping → call_ended → hangup)
    is exercised exactly as it is in production."""

    def __init__(self, *, on_outbound=None) -> None:  # noqa: ANN001
        self.sent: list[ServerMessage] = []
        self.cleared = 0
        self.hangups = 0
        self.customer_turns = 0
        self.is_bot_speaking = False
        self.stopped = False
        self._on_outbound = on_outbound

    async def run(self) -> None:
        return  # tests drive the session's methods directly, not a real socket

    async def send(self, message: ServerMessage) -> None:
        self.sent.append(message)
        if message.type in (ServerMessageType.audio_chunk, ServerMessageType.filler):
            self.is_bot_speaking = True
        elif message.type == ServerMessageType.tts_kill:
            self.is_bot_speaking = False
        if self._on_outbound is not None:
            await self._on_outbound(message)

    async def send_clear(self) -> None:
        self.cleared += 1
        self.is_bot_speaking = False

    def mark_customer_turn(self) -> None:
        self.customer_turns += 1
        self.is_bot_speaking = False

    async def hangup(self) -> None:
        self.hangups += 1
        self.stopped = True

    def transcripts_of(self, speaker: str) -> list[str]:
        return [
            str(m.data.get("text"))
            for m in self.sent
            if m.type == ServerMessageType.transcript and m.data.get("speaker") == speaker
        ]

    def partials(self) -> list[str]:
        return [
            str(m.data.get("text")) for m in self.sent if m.type == ServerMessageType.partial_transcript
        ]


class FakeIngest:
    def __init__(self) -> None:
        self.flushed = 0
        self.closed = False

    async def flush(self) -> None:
        self.flushed += 1

    async def aclose(self) -> None:
        self.closed = True


def _settings(**over):
    base = get_voicebot_settings()
    return base.model_copy(update=over)


def _state(session_id: str = "tw_sess_1", **over) -> SessionState:
    return SessionState(
        session_id=session_id, bot_id="bot_test", customer_id="cust_x",
        use_case="credit_card", language=Language.hinglish, **over,
    )


def _nlp(intent=Intent.interested, *, end_call=False, escalate=False) -> NlpResponse:
    return NlpResponse(
        session_id="tw_sess_1", response_text="Great, aap sun rahe hain?", intent=intent,
        tone=Tone.cheerful, escalate=escalate, end_call=end_call, confidence=0.9,
    )


def _stt(text: str, session_id: str = "tw_sess_1") -> TranscriptResult:
    return TranscriptResult(
        session_id=session_id, text=text, is_final=True,
        language_id=Language.hinglish, language_confidence=0.95,
        speech_duration_ms=800, word_confidence=0.9,
    )


def _session(
    state, clients, *, store=None, settings=None, smart_turn=None,
    escalations=None, monitor=None, background_tasks=None,
) -> tuple[TelephonyCallSession, FakeBridge]:
    settings = settings or _settings()
    orch = Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())
    bargein = BargeInClassifier(
        backchannel_max_speech_ms=settings.backchannel_max_speech_ms,
        min_interrupt_words=settings.min_interrupt_words,
        single_word_interrupt_ms=settings.single_word_interrupt_ms,
    )
    session = TelephonyCallSession(
        state=state, settings=settings, store=store if store is not None else FakeStore(),
        orchestrator=orch, bargein=bargein,
        clients=clients, smart_turn=smart_turn,
        escalations=escalations if escalations is not None else EscalationHub(),
        monitor=monitor if monitor is not None else LiveMonitor(),
        background_tasks=background_tasks if background_tasks is not None else set(),
    )
    bridge = FakeBridge(on_outbound=session.on_outbound)
    session.bind(bridge=bridge, ingest=FakeIngest())
    return session, bridge


# --- basic turn handling + waiting_agent / ended gates ----------------------


async def test_on_utterance_runs_a_normal_turn():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)

    await session.on_utterance(_stt("mera EMI kitna hai"), b"\x00\x01" * 100)

    assert bridge.customer_turns == 1
    assert bridge.transcripts_of("customer") == ["mera EMI kitna hai"]
    assert bridge.transcripts_of("bot")  # the reply turn was spoken


async def test_on_utterance_dropped_while_waiting_agent():
    state = _state()
    state.status = CallStatus.waiting_agent
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)

    await session.on_utterance(_stt("hello?"), b"\x00\x01" * 100)

    assert bridge.customer_turns == 0
    assert bridge.sent == []


async def test_on_utterance_dropped_once_call_has_ended():
    state = _state()
    state.status = CallStatus.ended
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)

    await session.on_utterance(_stt("hello?"), b"\x00\x01" * 100)

    assert bridge.sent == []


# --- smart-turn hold + merge -------------------------------------------------


async def test_smart_turn_holds_then_merges_a_followup():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    smart_turn = SmartTurnStub([False])  # first utterance: audio says "not done"
    session, bridge = _session(state, clients, smart_turn=smart_turn)

    await session.on_utterance(_stt("mujhe lagta hai ki"), b"\x00\x01" * 100)
    assert session._pending_turn is not None  # noqa: SLF001
    assert bridge.sent == []  # held — no turn ran yet

    await session.on_utterance(_stt("mera EMI zyada hai"), b"\x00\x01" * 100)

    assert session._pending_turn is None  # noqa: SLF001
    expected = merge_transcripts("mujhe lagta hai ki", "mera EMI zyada hai")
    assert bridge.transcripts_of("customer") == [expected]


async def test_smart_turn_flushes_after_hold_window_with_no_followup():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    smart_turn = SmartTurnStub([False])
    settings = _settings(smart_turn_hold_ms=10)
    session, bridge = _session(state, clients, smart_turn=smart_turn, settings=settings)

    await session.on_utterance(_stt("bas ek second..."), b"\x00\x01" * 100)
    assert bridge.sent == []

    await asyncio.sleep(0.05)

    assert bridge.transcripts_of("customer") == ["bas ek second..."]


async def test_no_smart_turn_configured_runs_immediately():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients, smart_turn=None)

    await session.on_utterance(_stt("haan bilkul"), b"\x00\x01" * 100)

    assert bridge.transcripts_of("customer") == ["haan bilkul"]


# --- speculative RAG (O-5) ---------------------------------------------------


async def test_speculative_retrieval_fires_during_hold_and_is_reused():
    state = _state(rag_enabled=True, rag_index="kb1")
    # build_speculative_context reads transcript[-1] — seed it as greet() would
    # have on a real call (the session never calls greet itself; the bridge does).
    state.add_turn(Turn(index=0, speaker="bot", text="Namaste, kaise madad karoon?"))
    clients = ScriptedClients(
        nlp=_nlp(), retrieve_chunks=[RetrievedChunk(title="EMI FAQ", content="...", score=0.9)]
    )
    smart_turn = SmartTurnStub([False])
    settings = _settings(speculative_rag_enabled=True)
    session, bridge = _session(state, clients, smart_turn=smart_turn, settings=settings)

    await session.on_utterance(_stt("mujhe emi ke baare mein"), b"\x00\x01" * 100)
    await asyncio.sleep(0)  # let the background retrieval task actually run
    assert clients.retrieve_calls  # fired in the background during the hold

    await session.on_utterance(_stt("puchna tha"), b"\x00\x01" * 100)

    assert bridge.transcripts_of("customer")  # the merged turn still completed normally


# --- human handoff (3.1): registration + on-hold gating ----------------------


async def test_escalation_registers_gates_further_turns_and_resolves(test_db):
    state = _state()
    clients = ScriptedClients(nlp=_nlp(escalate=True))
    escalations = EscalationHub()
    session, bridge = _session(state, clients, escalations=escalations)

    await session.on_utterance(_stt("mujhe operator se baat karni hai"), b"\x00\x01" * 100)

    assert state.status == CallStatus.waiting_agent
    assert escalations.is_live(state.session_id)
    turns_so_far = len(bridge.transcripts_of("customer"))

    # A further utterance while on hold must NOT produce another bot turn —
    # this is the exact gap the audit found: telephony used to keep answering
    # over the human handoff because nothing gated on waiting_agent.
    await session.on_utterance(_stt("hello, koi hai?"), b"\x00\x01" * 100)
    assert len(bridge.transcripts_of("customer")) == turns_so_far

    delivered = await escalations.deliver(state.session_id, "Operator here, how can I help?")
    assert delivered is True
    assert "Operator here, how can I help?" in bridge.transcripts_of("bot")

    resolved = await escalations.resolve(state.session_id)
    assert resolved is True
    assert any(m.type == ServerMessageType.call_ended for m in bridge.sent)
    # The generic call_ended → _end_call_leg hook actually hung up the PSTN leg.
    assert bridge.hangups == 1


# --- no-input watchdog --------------------------------------------------------


async def test_handle_no_input_reprompts_then_closes():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)

    await session._handle_no_input()  # noqa: SLF001
    assert state.no_input_strikes == 1
    assert state.status != CallStatus.ended
    assert bridge.transcripts_of("bot")  # the re-prompt was spoken

    await session._handle_no_input()  # noqa: SLF001
    assert state.outcome == "customer_unresponsive"
    assert state.status == CallStatus.ended
    assert any(m.type == ServerMessageType.call_ended for m in bridge.sent)
    assert bridge.hangups == 1


async def test_no_input_watchdog_fires_a_reprompt(monkeypatch):
    import koel.voicebot.telephony.session as session_mod

    monkeypatch.setattr(session_mod, "_NO_INPUT_POLL_INTERVAL_S", 0.01)
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(telephony_no_input_reprompt_ms=20, telephony_no_input_close_ms=20)
    session, bridge = _session(state, clients, settings=settings)

    task = asyncio.create_task(session._no_input_watchdog())  # noqa: SLF001
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert state.no_input_strikes >= 1
    assert bridge.transcripts_of("bot")


async def test_no_input_watchdog_skips_while_on_hold():
    state = _state()
    state.status = CallStatus.waiting_agent
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)

    task = asyncio.create_task(session._no_input_watchdog())  # noqa: SLF001
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert state.no_input_strikes == 0
    assert bridge.sent == []


# --- barge-in Gate 1 (RMS voice-activity) + Gate 2 (STT partial) ------------


async def test_voice_activity_confirms_a_sustained_interrupt():
    state = _state()
    state.add_turn(Turn(index=0, speaker="bot", text="hello there"))
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(single_word_interrupt_ms=10)
    session, bridge = _session(state, clients, settings=settings)
    bridge.is_bot_speaking = True

    await session._confirm_sustained_voice_activity()  # noqa: SLF001

    assert bridge.cleared == 1
    assert state.transcript[-1].interrupted is True


async def test_voice_activity_does_not_interrupt_if_bot_stopped_speaking():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(single_word_interrupt_ms=10)
    session, bridge = _session(state, clients, settings=settings)
    bridge.is_bot_speaking = True

    task = asyncio.create_task(session._confirm_sustained_voice_activity())  # noqa: SLF001
    await asyncio.sleep(0)  # let it start sleeping
    bridge.is_bot_speaking = False  # the turn resolved normally in the meantime
    await task

    assert bridge.cleared == 0


async def test_on_voice_activity_noop_when_bot_not_speaking():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)
    bridge.is_bot_speaking = False

    await session.on_voice_activity()
    await asyncio.sleep(0.01)

    assert bridge.cleared == 0


async def test_on_partial_backchannel_does_not_interrupt():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)
    bridge.is_bot_speaking = True

    await session.on_partial("haan")  # a backchannel word

    assert bridge.cleared == 0
    assert bridge.partials() == ["haan"]  # still forwarded as a live caption


async def test_on_partial_sustained_speech_interrupts():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(backchannel_max_speech_ms=10)
    session, bridge = _session(state, clients, settings=settings)
    bridge.is_bot_speaking = True

    # The gate's elapsed-time tracker self-starts on the FIRST call (duration
    # 0ms — always "too_short"), so a real interrupt needs a primed call, real
    # elapsed time, then a second call — mirrors test_telephony_bargein_gate.py.
    await session.on_partial("mujhe")
    await asyncio.sleep(0.05)
    await session.on_partial("mujhe abhi baat karni hai isi waqt")  # long, non-backchannel

    assert bridge.cleared == 1


async def test_on_partial_resets_gate_when_bot_not_speaking():
    state = _state()
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)
    bridge.is_bot_speaking = False

    await session.on_partial("hello")

    assert bridge.cleared == 0
    assert bridge.partials() == ["hello"]


# --- end-of-call persistence (the critical gap: telephony calls were never
# written to Postgres at all) -------------------------------------------------


async def test_run_persists_the_call_and_cleans_up_redis(test_db):
    state = _state("tw_finalize_1")
    clients = ScriptedClients(nlp=_nlp())
    store = FakeStore()
    session, bridge = _session(state, clients, store=store)

    await session.run()

    assert store.deleted == ["tw_finalize_1"]
    async with test_db() as db:
        call = (
            await db.execute(select(CallLog).where(CallLog.session_id == "tw_finalize_1"))
        ).scalar_one()
    assert call.bot_id == "bot_test"


async def test_run_flushes_the_ingest_unless_already_ended(test_db):
    state = _state("tw_finalize_2")
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)
    ingest = session._ingest  # noqa: SLF001

    await session.run()

    assert ingest.flushed == 1
    assert ingest.closed is True


async def test_run_skips_flush_when_call_already_ended(test_db):
    state = _state("tw_finalize_3")
    state.status = CallStatus.ended
    clients = ScriptedClients(nlp=_nlp())
    session, bridge = _session(state, clients)
    ingest = session._ingest  # noqa: SLF001

    await session.run()

    assert ingest.flushed == 0
    assert ingest.closed is True


# --- bridge hooks -------------------------------------------------------------


async def test_on_call_started_persists_state_to_redis():
    state = _state("tw_started_1")
    clients = ScriptedClients(nlp=_nlp())
    store = FakeStore()
    session, _bridge = _session(state, clients, store=store)
    state.twilio_call_sid = "CA999"

    await session.on_call_started()

    assert store.data["tw_started_1"].twilio_call_sid == "CA999"


async def test_end_call_leg_skips_rest_hangup_when_bridge_already_stopped(monkeypatch):
    calls = []
    # Never actually called on this path (asserted below) — a plain, unawaited
    # callable is enough to prove that.
    monkeypatch.setattr(
        "koel.voicebot.telephony.session.end_twilio_call", lambda **kw: calls.append(kw)
    )
    state = _state("tw_hangup_1")
    state.twilio_call_sid = "CA1"
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(twilio_account_sid="AC1", twilio_auth_token="tok")
    session, bridge = _session(state, clients, settings=settings)
    bridge.stopped = True

    await session._end_call_leg()  # noqa: SLF001

    assert bridge.hangups == 1
    assert calls == []  # Twilio already knows — no REST call needed


async def test_end_call_leg_calls_rest_hangup_when_orchestrator_initiated(monkeypatch):
    calls = []

    async def _fake_end(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr("koel.voicebot.telephony.session.end_twilio_call", _fake_end)
    state = _state("tw_hangup_2")
    state.twilio_call_sid = "CA2"
    clients = ScriptedClients(nlp=_nlp())
    settings = _settings(twilio_account_sid="AC1", twilio_auth_token="tok")
    session, bridge = _session(state, clients, settings=settings)
    bridge.stopped = False

    await session._end_call_leg()  # noqa: SLF001

    assert bridge.hangups == 1
    assert calls == [{"call_sid": "CA2", "account_sid": "AC1", "auth_token": "tok"}]
