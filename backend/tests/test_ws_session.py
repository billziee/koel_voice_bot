"""End-to-end WebSocket session drive (greeting → turn → barge-in → end/disconnect).

Exercises the CallSession receive loop + dispatch + orchestrator turn flow with a
scripted fake socket, covering the paths that per-file coverage showed untested.
"""

from __future__ import annotations

import asyncio
import base64
import json

from fastapi import WebSocketDisconnect

from koel.common.schemas import Language, Tone
from koel.nlp.schemas import Intent, NlpResponse
from koel.stt.schemas import TranscriptResult
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.ws.handler import CallSession
from koel.voicebot.ws.protocol import ServerMessageType
from tests.conftest import FakeClients, FakeStore


class ScriptedClients(FakeClients):
    """FakeClients with a canned STT transcript + NLP response for turn tests."""

    def __init__(self, *, nlp: NlpResponse, stt_text: str = "haan boliye") -> None:
        super().__init__()
        self._nlp = nlp
        self._stt_text = stt_text

    async def transcribe(self, request):  # noqa: ANN001
        return TranscriptResult(
            session_id=request.session_id,
            text=self._stt_text,
            is_final=request.is_final,
            language_id=Language.hinglish,
            language_confidence=0.95,
            speech_duration_ms=800,
            word_confidence=0.9,
        )

    async def respond(self, ctx):  # noqa: ANN001
        return self._nlp.model_copy(update={"session_id": ctx.session_id})


class FakeWebSocket:
    """Scripts inbound client frames; records outbound server frames."""

    def __init__(self, incoming: list[str]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []
        self.headers: dict[str, str] = {}
        self.accepted_subprotocol = "<not-accepted>"
        self.closed: tuple | None = None

    async def accept(self, subprotocol=None) -> None:  # noqa: ANN001
        self.accepted_subprotocol = subprotocol

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect(code=1006)

    async def close(self, code=None, reason=None) -> None:  # noqa: ANN001
        self.closed = (code, reason)

    def frames_of(self, t: ServerMessageType) -> list[dict]:
        return [f["data"] for f in self.sent if f["type"] == t.value]


def _state(session_id="ws_1") -> SessionState:
    return SessionState(
        session_id=session_id,
        bot_id="bot_test",
        customer_id="cust_x",
        use_case="credit_card",
        greeting_template="Namaste {name} ji.",
        crm_context={"name": "Rahul"},
    )


def _nlp(intent=Intent.interested, *, end_call=False, escalate=False) -> NlpResponse:
    return NlpResponse(
        session_id="ws_1", response_text="Great, aap sun rahe hain?", intent=intent,
        tone=Tone.cheerful, escalate=escalate, end_call=end_call, confidence=0.9,
    )


def _session(ws, state, clients, **over) -> CallSession:
    from koel.voicebot.pipeline.bargein import BargeInClassifier
    from koel.voicebot.pipeline.filler import FillerCache
    from koel.voicebot.pipeline.orchestrator import Orchestrator

    settings = VoiceBotSettings(heartbeat_interval_seconds=999, **over)
    orch = Orchestrator(settings=settings, clients=clients, filler_cache=FillerCache())
    return CallSession(
        websocket=ws, state=state, settings=settings, store=FakeStore(),
        orchestrator=orch, bargein=BargeInClassifier(backchannel_max_speech_ms=300),
        clients=clients, resumed=False,
    )


async def test_greeting_then_full_turn_and_disconnect(test_db):
    audio = json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})
    ws = FakeWebSocket([audio])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state(), clients)

    await call.run()

    # Greeting spoken (self-identifies as automated), customer + bot turns transcribed.
    transcripts = ws.frames_of(ServerMessageType.transcript)
    speakers = [t["speaker"] for t in transcripts]
    assert speakers[0] == "bot"  # greeting
    assert "customer" in speakers and speakers.count("bot") >= 2
    greeting = transcripts[0]["text"].lower()
    assert "automated" in greeting or "koel" in greeting
    # Audio was synthesized both for the greeting and the reply.
    assert len(ws.frames_of(ServerMessageType.audio_chunk)) >= 2
    # Disconnect (empty queue) routed to the grace path, not a crash.
    assert call._closed or call._disconnect_started  # noqa: SLF001


async def test_end_call_frame_closes_and_emits_call_ended(test_db):
    ws = FakeWebSocket([json.dumps({"type": "end_call"})])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_end"), clients)

    await call.run()

    assert ws.frames_of(ServerMessageType.call_ended)
    assert call._closed  # noqa: SLF001


async def test_malformed_frame_yields_error_then_continues(test_db):
    ws = FakeWebSocket(["{not valid json", json.dumps({"type": "end_call"})])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_bad"), clients)

    await call.run()

    errors = ws.frames_of(ServerMessageType.error)
    assert any(e["code"] == "bad_message" for e in errors)


async def test_playback_done_starts_no_input_clock_and_is_not_input(test_db):
    # playback_done sets speech_until≈now (bot truly stopped) without counting as
    # customer input — so the no-input window begins at real playback end.
    ws = FakeWebSocket([json.dumps({"type": "playback_done"})])
    call = _session(ws, _state("ws_pbd"), ScriptedClients(nlp=_nlp()))
    call._last_input_at = 1000.0  # noqa: SLF001 — sentinel
    call._speech_until = 9e9  # noqa: SLF001 — far-future estimate
    await call.run()
    assert call._speech_until < 9e9  # noqa: SLF001 — reset to ~now
    assert call._last_input_at == 1000.0  # noqa: SLF001 — NOT treated as input


async def test_confirmed_barge_in_sends_tts_kill(test_db):
    # A long, non-backchannel interrupt during bot speech → TTS_KILL.
    bargein = json.dumps({
        "type": "barge_in_candidate",
        "data": {"partial_text": "ruko ruko mujhe sunna hai poori baat", "speech_duration_ms": 1500},
    })
    ws = FakeWebSocket([bargein])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_barge"), clients)

    await call.run()

    assert ws.frames_of(ServerMessageType.tts_kill), "a real interrupt must flush playback"


async def test_opt_out_intent_ends_call_immediately(test_db):
    audio = json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})
    ws = FakeWebSocket([audio])
    clients = ScriptedClients(nlp=_nlp(Intent.opt_out, end_call=True), stt_text="mujhe DND karo")
    state = _state("ws_optout")
    call = _session(ws, state, clients)

    await call.run()

    # TRAI opt-out flags the CRM and ends the call.
    assert any(f[1] == "opt_out" for f in clients.flags)
    assert state.status == CallStatus.ended


async def test_opt_out_honored_even_when_crm_flag_fails(test_db):
    # Audit #10: a CRM outage must NOT prevent honouring the opt-out.
    audio = json.dumps({"type": "audio_chunk", "audio_b64": "x", "is_final": True})
    ws = FakeWebSocket([audio])
    clients = ScriptedClients(nlp=_nlp(Intent.opt_out, end_call=True), stt_text="mujhe DND karo")

    async def boom(*a, **k):
        raise RuntimeError("crm down")

    clients.flag_customer = boom  # type: ignore[method-assign]
    state = _state("ws_optout_crmdown")
    call = _session(ws, state, clients)

    await call.run()  # must not raise

    assert state.status == CallStatus.ended  # opt-out still honoured + call ended
    assert ws.frames_of(ServerMessageType.call_ended)


async def test_no_input_service_failure_fails_call_not_crash(test_db):
    # Audit #5/#6: a TTS failure during the no-input reprompt must be caught by
    # _safe_no_input (error frame + finalize), never crash the socket task.
    ws = FakeWebSocket([])
    clients = ScriptedClients(nlp=_nlp())

    async def boom(*a, **k):
        raise RuntimeError("tts down")

    async def boom_stream(*a, **k):  # streaming path (stream_tts default on)
        raise RuntimeError("tts down")
        yield  # pragma: no cover — makes this an async generator

    clients.synthesize = boom  # type: ignore[method-assign]
    clients.stream_synthesis = boom_stream  # type: ignore[method-assign]
    call = _session(ws, _state("ws_noinput_fail"), clients)

    await call._safe_no_input()  # noqa: SLF001 — must swallow → _fail_call, not raise

    assert call._closed  # noqa: SLF001
    assert any(e["code"] == "no_input_failed" for e in ws.frames_of(ServerMessageType.error))


async def test_call_summary_skipped_when_sarvam_disabled(test_db):
    # Default settings have no sarvam_api_key — must not attempt a summary call.
    ws = FakeWebSocket([json.dumps({"type": "end_call"})])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_summary_off"), clients)

    await call.run()
    await asyncio.gather(*call._background_tasks)  # noqa: SLF001 — drain any scheduled tasks

    assert clients.call_summaries == []


async def test_call_summary_runs_when_sarvam_enabled(monkeypatch, test_db):
    from koel.voicebot.pipeline.summarizer import CallSummary

    class StubSummarizer:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            pass

        async def summarize(self, turns) -> CallSummary:  # noqa: ANN001
            return CallSummary(
                interest_shown=True, objection="", sentiment="positive", next_action="Follow up"
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("koel.voicebot.pipeline.postcall.SarvamSummarizer", StubSummarizer)

    ws = FakeWebSocket([json.dumps({"type": "end_call"})])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_summary_on"), clients, sarvam_api_key="k")

    await call.run()
    await asyncio.gather(*call._background_tasks)  # noqa: SLF001 — drain the fire-and-forget task

    assert len(clients.call_summaries) == 1
    customer_id, summary = clients.call_summaries[0]
    assert customer_id == "cust_x"
    assert summary["interest_shown"] is True
    assert summary["next_action"] == "Follow up"


# --- full-conversation call recording (both sides) --------------------------


def _audio_chunk_with_pcm(is_final: bool = True) -> str:
    pcm = base64.b64encode(b"\x00\x01" * 160).decode()
    return json.dumps({
        "type": "audio_chunk", "audio_b64": "webm-opus-stand-in", "is_final": is_final,
        "pcm16_b64": pcm,
    })


async def test_customer_audio_is_buffered_alongside_bot_audio(test_db):
    from koel.tts.mixer import concat_wavs

    ws = FakeWebSocket([_audio_chunk_with_pcm()])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_rec_both"), clients)

    await call.run()

    # Both the bot's turns (greeting + reply) AND the customer's utterance
    # ended up in the recording buffer — not just the bot's half.
    assert len(call._audio_buffer) >= 3  # noqa: SLF001
    # And it actually muxes into one real, non-empty recording.
    assert concat_wavs(call._audio_buffer)  # noqa: SLF001


async def test_customer_audio_skipped_without_neural_vad_pcm(test_db):
    # Older client / real mic capture with no Silero VAD worklet running has
    # no pcm16_b64 at all — best-effort degrades to bot-only, never crashes.
    audio = json.dumps({"type": "audio_chunk", "audio_b64": "webm-opus", "is_final": True})
    ws = FakeWebSocket([audio])
    clients = ScriptedClients(nlp=_nlp())
    call = _session(ws, _state("ws_rec_no_pcm"), clients)

    await call.run()

    assert len(call._audio_buffer) >= 2  # noqa: SLF001 — bot audio only (greeting + reply)
