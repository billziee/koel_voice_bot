"""Browser ``/ws/calls`` streaming STT (O-6 Wave 2) over a fake Deepgram session.

Hermetic — a :class:`FakeStreamingSession` records ``send_frame`` calls and lets the
test emit ``on_partial`` / ``on_final``; NO live key. Drives :class:`CallSession` in
streaming mode via an injected session factory and asserts:

  - inbound audio frames are forwarded to the session;
  - an ``on_partial`` emits a ``partial_transcript`` ServerMessage (best-effort caption);
  - an ``on_final`` reaches ``orchestrator.handle_final_transcript`` (→ a real turn);
  - a session ``start()`` failure degrades to the (unchanged) buffered per-utterance path;
  - teardown closes the session (no Deepgram socket leak).

Reuses the conftest fakes + the FakeWebSocket / ScriptedClients patterns from
``test_ws_session``.
"""

from __future__ import annotations

import asyncio
import base64
import json

from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.models import CallStatus
from koel.voicebot.ws.handler import CallSession
from koel.voicebot.ws.protocol import ClientMessage, ClientMessageType, ServerMessageType
from tests.conftest import FakeStore
from tests.test_ws_session import FakeWebSocket, ScriptedClients, _nlp, _state

# One valid raw-PCM16 frame (base64) — decodes to non-empty bytes so it's forwarded.
_PCM = base64.b64encode(b"\x00\x01" * 160).decode()


def _audio(is_final: bool) -> str:
    return json.dumps({"type": "audio_chunk", "pcm16_b64": _PCM, "is_final": is_final})


class FakeStreamingSession:
    """Stand-in for :class:`DeepgramStreamingSession`.

    Records forwarded frames; emits an interim per ``send_frame`` and a final on
    ``finish`` (so a ``run()`` drive surfaces both through the wired callbacks).
    ``fail_on_start`` injects a connect failure so the call must degrade to buffered.
    """

    def __init__(self, *, fail_on_start: bool = False) -> None:
        self.on_partial = None
        self.on_final = None
        self._fail_on_start = fail_on_start
        self.started = False
        self.finished = False
        self.closed = False
        self.frames: list[bytes] = []

    async def start(self) -> None:
        if self._fail_on_start:
            raise ConnectionRefusedError("deepgram unreachable")
        self.started = True

    async def send_frame(self, pcm16: bytes) -> None:
        self.frames.append(pcm16)
        if self.on_partial is not None:
            await self.on_partial(f"interim {len(self.frames)}")

    async def finish(self) -> None:
        self.finished = True
        if self.on_final is not None:
            await self.on_final(
                TranscriptResult(session_id="s", text="final utterance", is_final=True)
            )

    async def aclose(self) -> None:
        self.closed = True


def _factory_for(session: FakeStreamingSession):  # noqa: ANN202
    def factory(on_partial, on_final):  # noqa: ANN001, ANN202
        session.on_partial = on_partial
        session.on_final = on_final
        return session

    return factory


def _session(ws, state, clients, *, factory=None, **over) -> CallSession:  # noqa: ANN001
    settings = VoiceBotSettings(heartbeat_interval_seconds=999, **over)
    orch = Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())
    return CallSession(
        websocket=ws, state=state, settings=settings, store=FakeStore(),
        orchestrator=orch, bargein=BargeInClassifier(backchannel_max_speech_ms=300),
        clients=clients, resumed=False, streaming_session_factory=factory,
    )


async def test_streaming_forwards_frames_and_surfaces_partial_and_final(test_db):
    # Two frames: the first pushes audio (→ interim partial), the second carries the
    # client VAD end (is_final) → finish() → the session's final drives the turn.
    ws = FakeWebSocket([_audio(is_final=False), _audio(is_final=True)])
    clients = ScriptedClients(nlp=_nlp())
    session = FakeStreamingSession()
    call = _session(ws, _state("ws_stream"), clients, factory=_factory_for(session))

    await call.run()

    # Frames were pushed into the streaming session (not the batch transcribe path).
    assert len(session.frames) == 2
    assert session.finished is True
    # Interim partials surfaced as best-effort customer captions.
    partials = ws.frames_of(ServerMessageType.partial_transcript)
    assert partials and all(p["speaker"] == "customer" for p in partials)
    # The final drove the SAME downstream turn: a customer turn from the streaming
    # final (its text, NOT the batch transcribe stub) + a bot reply.
    transcripts = ws.frames_of(ServerMessageType.transcript)
    speakers = [t["speaker"] for t in transcripts]
    assert "customer" in speakers and speakers.count("bot") >= 2  # greeting + reply
    assert any(t["speaker"] == "customer" and t["text"] == "final utterance" for t in transcripts)


async def test_on_final_reaches_orchestrator(test_db):
    # Direct wiring: build the session via the factory, then fire the callback the
    # factory captured (proves the CallSession wired its own _on_stream_final in).
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp())
    session = FakeStreamingSession()
    call = _session(ws, _state("ws_final"), clients, factory=_factory_for(session))

    frame = ClientMessage(type=ClientMessageType.audio_chunk, pcm16_b64=_PCM, is_final=False)
    assert await call._stt_stream.feed(frame) is True  # noqa: SLF001 — opens the session
    assert session.started is True
    await session.on_final(TranscriptResult(session_id="s", text="haan boliye", is_final=True))

    transcripts = ws.frames_of(ServerMessageType.transcript)
    assert any(t["speaker"] == "customer" and t["text"] == "haan boliye" for t in transcripts)
    assert any(t["speaker"] == "bot" for t in transcripts)  # orchestrator produced a reply


async def test_concurrent_finals_are_serialized(test_db):
    # Finals arrive from the Deepgram reader task(s), not the single-driver receive
    # loop, so two close finals (old session finishing while the next is open) could
    # otherwise drive overlapping turns. The final lock must serialize them.
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp())
    session = FakeStreamingSession()
    call = _session(ws, _state("ws_serial"), clients, factory=_factory_for(session))

    active = 0
    max_active = 0
    ran = 0

    async def slow_turn(state, send, stt):  # noqa: ANN001 — probes turn overlap
        nonlocal active, max_active, ran
        active += 1
        max_active = max(max_active, active)
        ran += 1
        await asyncio.sleep(0.05)
        active -= 1

    call._orch.handle_final_transcript = slow_turn  # type: ignore[assignment] # noqa: SLF001

    def _fin(text):  # noqa: ANN001, ANN202
        return TranscriptResult(session_id="s", text=text, is_final=True)

    await asyncio.gather(
        call._stt_stream._on_final(_fin("pehli baat")),  # noqa: SLF001
        call._stt_stream._on_final(_fin("doosri baat")),  # noqa: SLF001
    )

    assert ran == 2  # both finals were processed (none dropped)
    assert max_active == 1  # but never concurrently — the lock serialized the turns


async def test_start_failure_degrades_to_buffered_path(test_db):
    # start() raises → the call must fall back to the buffered per-utterance transcribe
    # path (never drop the turn) for this frame and the rest of the call.
    ws = FakeWebSocket([_audio(is_final=True)])
    clients = ScriptedClients(nlp=_nlp(), stt_text="haan boliye")
    session = FakeStreamingSession(fail_on_start=True)
    call = _session(ws, _state("ws_degrade"), clients, factory=_factory_for(session))

    await call.run()

    assert session.started is False and session.frames == []  # nothing reached the session
    assert call._stt_stream.enabled is False  # noqa: SLF001 — start failure degraded it
    # Buffered path ran: the customer turn is the batch transcribe stub, plus a reply.
    transcripts = ws.frames_of(ServerMessageType.transcript)
    assert any(t["speaker"] == "customer" and t["text"] == "haan boliye" for t in transcripts)
    assert [t["speaker"] for t in transcripts].count("bot") >= 2


async def test_teardown_closes_session(test_db):
    # A frame opens the session; end_call finalizes the CallSession, which must close
    # the streaming session so the Deepgram socket isn't leaked.
    ws = FakeWebSocket([_audio(is_final=False), json.dumps({"type": "end_call"})])
    clients = ScriptedClients(nlp=_nlp())
    session = FakeStreamingSession()
    call = _session(ws, _state("ws_close"), clients, factory=_factory_for(session))

    await call.run()

    assert session.started is True and len(session.frames) == 1
    assert session.closed is True  # _finalize closed the long-lived session
    assert call._state.status == CallStatus.ended  # noqa: SLF001
