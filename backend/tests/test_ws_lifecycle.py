"""WS lifecycle paths: no-input reprompt→close, escalation, resume, disconnect grace."""

from __future__ import annotations

import asyncio
import json

from fastapi import WebSocketDisconnect

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.session.models import CallStatus, SessionState, Turn
from koel.voicebot.ws.handler import CallSession
from koel.voicebot.ws.protocol import ServerMessageType
from tests.conftest import FakeClients, FakeStore


class _Clients(FakeClients):
    def __init__(self, nlp: NlpResponse) -> None:
        super().__init__()
        self._nlp = nlp

    async def transcribe(self, request):  # noqa: ANN001
        return TranscriptResult(
            session_id=request.session_id, text="haan", is_final=request.is_final,
            language_id=Language.hinglish, language_confidence=0.95,
            speech_duration_ms=600, word_confidence=0.9,
        )

    async def respond(self, ctx):  # noqa: ANN001
        return self._nlp.model_copy(update={"session_id": ctx.session_id})


class FakeWS:
    def __init__(self, incoming: list[str], *, block_after_empty: bool = False) -> None:
        self._incoming = list(incoming)
        self._block = block_after_empty
        self.sent: list[dict] = []
        self.headers: dict[str, str] = {}
        self.closed = None

    async def accept(self, subprotocol=None) -> None:  # noqa: ANN001
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        if self._block:
            await asyncio.Event().wait()  # blocks → wait_for times out → no-input path
        raise WebSocketDisconnect(code=1006)

    async def close(self, code=None, reason=None) -> None:  # noqa: ANN001
        self.closed = (code, reason)

    def types(self) -> list[str]:
        return [f["type"] for f in self.sent]


def _nlp(intent=Intent.interested, **over) -> NlpResponse:
    base = dict(session_id="s", response_text="ok sun rahe hain?", intent=intent,
                tone=Tone.cheerful, confidence=0.9)
    base.update(over)
    return NlpResponse(**base)


def _state(sid="lc", **over) -> SessionState:
    base = dict(session_id=sid, bot_id="bot_test", customer_id="cust_x", use_case="credit_card",
                greeting_template="Namaste ji.", crm_context={"name": "Rahul"})
    base.update(over)
    return SessionState(**base)


def _session(ws, state, clients, store=None, resumed=False, **over) -> CallSession:
    from koel.voicebot.pipeline.bargein import BargeInClassifier
    from koel.voicebot.pipeline.filler import FillerCache
    from koel.voicebot.pipeline.orchestrator import Orchestrator

    settings = VoiceBotSettings(heartbeat_interval_seconds=999, **over)
    orch = Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())
    return CallSession(
        websocket=ws, state=state, settings=settings, store=store or FakeStore(),
        orchestrator=orch, bargein=BargeInClassifier(backchannel_max_speech_ms=300),
        clients=clients, resumed=resumed,
    )


async def test_no_input_reprompts_then_closes(test_db):
    ws = FakeWS([], block_after_empty=True)
    # High speaking_rate shrinks the stub audio duration so the timer (which
    # correctly waits for playback to finish) doesn't make the test slow.
    state = _state("lc_noinput", speaking_rate=12.0)
    call = _session(ws, state, _Clients(_nlp()), no_input_reprompt_ms=5, no_input_close_ms=5)
    await asyncio.wait_for(call.run(), timeout=15)
    # First timeout re-prompts, second closes the call as unresponsive.
    assert call._state.no_input_strikes >= 1  # noqa: SLF001
    assert call._closed  # noqa: SLF001


async def test_escalation_intent_hands_off(test_db):
    ws = FakeWS([json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})])
    state = _state("lc_esc")
    call = _session(ws, state, _Clients(_nlp(Intent.request_human, escalate=True,
                                             escalation_reason="customer asked for human")))
    await call.run()
    assert state.escalated is True


async def test_resumed_connection_emits_reconnected(test_db):
    state = _state("lc_re")
    state.status = CallStatus.reconnecting
    state.add_turn(Turn(index=0, speaker="bot", text="Namaste"))
    ws = FakeWS([])  # no frames → immediate disconnect after the resume banner
    call = _session(ws, state, _Clients(_nlp()), resumed=True)
    await call.run()
    assert ServerMessageType.reconnected.value in ws.types()


async def test_disconnect_starts_grace_not_crash(test_db):
    state = _state("lc_drop")
    store = FakeStore()
    await store.save(state)
    ws = FakeWS([])  # empty → WebSocketDisconnect on first receive
    call = _session(ws, state, _Clients(_nlp()), store=store)
    await call.run()
    # Routed to the grace path (no exception escaped run()).
    assert call._disconnect_started or call._closed  # noqa: SLF001
