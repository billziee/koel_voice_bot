"""Twilio Media Streams ↔ orchestrator bridge (roadmap 1.1 + 1.3).

Drives :class:`TwilioMediaBridge` with a fake WebSocket (scripted Twilio JSON
frames) and a recording fake orchestrator — no carrier, no STT, no Redis. Waits
are deterministic: ``iter_text`` yields the scripted frames then stops, so
``run()`` returns once every frame is processed.
"""

from __future__ import annotations

import base64
import io
import json
import wave
from collections.abc import AsyncIterator

from koel.stt.schemas import TranscriptResult
from koel.voicebot.session.models import SessionState
from koel.voicebot.telephony.bridge import TwilioMediaBridge, run_twilio_bridge
from koel.voicebot.telephony.codec import ulaw8k_to_pcm16
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

# --- fakes ------------------------------------------------------------------

class FakeTwilioWS:
    """Yields scripted inbound frames; records outbound frames (parsed)."""

    def __init__(self, incoming: list[str]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    async def iter_text(self) -> AsyncIterator[str]:
        for frame in self._incoming:
            yield frame

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def frames_of(self, event: str) -> list[dict]:
        return [f for f in self.sent if f.get("event") == event]


class RecordingOrchestrator:
    """Structurally an ``OrchestratorLike`` + inbound-audio sink; records calls
    and emits preset outbound ``ServerMessage``s so wire translation is testable."""

    def __init__(
        self,
        *,
        greet_messages: list[ServerMessage] | None = None,
        reply_messages: list[ServerMessage] | None = None,
    ) -> None:
        self._greet_messages = greet_messages or []
        self._reply_messages = reply_messages or []
        self.greeted = False
        self.turns: list[TranscriptResult] = []
        self.audio: list[tuple[bytes, int]] = []
        self.ended: str | None = None

    async def greet(self, state, send) -> None:  # noqa: ANN001
        self.greeted = True
        for msg in self._greet_messages:
            await send(msg)

    async def handle_final_transcript(self, state, send, stt) -> None:  # noqa: ANN001
        self.turns.append(stt)
        for msg in self._reply_messages:
            await send(msg)

    async def end_call(self, state, send, *, outcome: str) -> None:  # noqa: ANN001
        self.ended = outcome

    async def feed_audio(self, pcm16: bytes, sample_rate: int) -> None:
        self.audio.append((pcm16, sample_rate))


# --- helpers ----------------------------------------------------------------

def _state(session_id: str = "tw_1") -> SessionState:
    return SessionState(session_id=session_id, bot_id="bot_test", use_case="credit_card")


def _wav_b64(pcm: bytes, rate: int = 8000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


def _connected() -> str:
    return json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})


def _start(sid: str) -> str:
    return json.dumps({
        "event": "start",
        "streamSid": sid,
        "start": {
            "streamSid": sid,
            "callSid": "CA1",
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    })


def _media(sid: str, ulaw: bytes) -> str:
    return json.dumps({
        "event": "media",
        "streamSid": sid,
        "media": {"payload": base64.b64encode(ulaw).decode()},
    })


def _dtmf(sid: str, digit: str) -> str:
    return json.dumps({"event": "dtmf", "streamSid": sid, "dtmf": {"digit": digit}})


def _stop(sid: str) -> str:
    return json.dumps({"event": "stop", "streamSid": sid})


# --- lifecycle + outbound audio --------------------------------------------

async def test_start_greets_and_emits_a_twilio_media_frame():
    greet_wav = _wav_b64(b"\x01\x00" * 80, rate=8000)
    orch = RecordingOrchestrator(
        greet_messages=[
            ServerMessage.of(
                ServerMessageType.audio_chunk, audio_b64=greet_wav, audio_format="wav", sample_rate=8000
            )
        ]
    )
    ws = FakeTwilioWS([_connected(), _start("MZ1")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state(), on_caller_audio=orch.feed_audio)

    assert orch.greeted is True
    media = ws.frames_of("media")
    assert len(media) == 1
    assert media[0]["streamSid"] == "MZ1"
    # A real, non-empty µ-law payload was produced from the greeting WAV.
    assert base64.b64decode(media[0]["media"]["payload"])


async def test_outbound_ulaw_audio_passes_through_unchanged():
    ulaw = bytes(range(40))  # already 8 kHz µ-law (telephony-mode synthesis)
    orch = RecordingOrchestrator(
        greet_messages=[
            ServerMessage.of(
                ServerMessageType.audio_chunk,
                audio_b64=base64.b64encode(ulaw).decode(),
                audio_format="ulaw",
                sample_rate=8000,
            )
        ]
    )
    ws = FakeTwilioWS([_start("MZ2")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state())

    media = ws.frames_of("media")
    assert len(media) == 1
    assert base64.b64decode(media[0]["media"]["payload"]) == ulaw


async def test_barge_in_tts_kill_emits_a_clear_frame():
    orch = RecordingOrchestrator(
        greet_messages=[ServerMessage.of(ServerMessageType.tts_kill, reason="interrupt")]
    )
    ws = FakeTwilioWS([_start("MZ3")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state())

    clears = ws.frames_of("clear")
    assert len(clears) == 1
    assert clears[0] == {"event": "clear", "streamSid": "MZ3"}


# --- inbound media ----------------------------------------------------------

async def test_inbound_media_is_decoded_to_pcm_for_the_orchestrator():
    ulaw = b"\xff" * 160  # 20ms of 8 kHz µ-law
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ4"), _media("MZ4", ulaw)])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state(), on_caller_audio=orch.feed_audio)

    assert len(orch.audio) == 1
    pcm, rate = orch.audio[0]
    assert rate == 16000
    assert len(pcm) > 0
    # Exactly the codec's decode — the bridge adds no processing of its own.
    assert pcm == ulaw8k_to_pcm16(ulaw, 16000)


async def test_media_without_an_audio_hook_is_dropped_not_crashed():
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ4"), _media("MZ4", b"\xff" * 80)])

    # No on_caller_audio wired → media is decoded-and-dropped, never an error.
    await run_twilio_bridge(ws, orchestrator=orch, state=_state())

    assert orch.audio == []


# --- DTMF (1.3) -------------------------------------------------------------

async def test_dtmf_digit_injects_a_synthetic_customer_turn():
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ5"), _dtmf("MZ5", "1")])

    await run_twilio_bridge(
        ws, orchestrator=orch, state=_state(), dtmf_map={"1": "Yes, I agree", "2": "No"}
    )

    assert len(orch.turns) == 1
    turn = orch.turns[0]
    assert turn.text == "Yes, I agree"
    assert turn.is_final is True
    # High confidence + in-language so the unsupported-language guard can't fire.
    assert turn.language_confidence == 1.0


async def test_unmapped_dtmf_digit_is_ignored():
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ6"), _dtmf("MZ6", "9")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state(), dtmf_map={"1": "Yes"})

    assert orch.turns == []


# --- resilience + lifecycle -------------------------------------------------

async def test_malformed_frame_does_not_crash_the_call():
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ7"), "{ not valid json", _dtmf("MZ7", "1")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state(), dtmf_map={"1": "Yes"})

    # The bad frame was skipped; the following valid DTMF still produced a turn.
    assert len(orch.turns) == 1
    assert orch.turns[0].text == "Yes"


async def test_stop_event_ends_the_call():
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ8"), _stop("MZ8")])

    await run_twilio_bridge(ws, orchestrator=orch, state=_state())

    assert orch.ended == "completed"


async def test_abnormal_disconnect_marks_call_dropped():
    # Regression: the carrier socket drops WITHOUT a Twilio `stop` (iter_text ends
    # on its own). The bridge must mark the call `call_dropped` / ended so the
    # owning route persists the true outcome instead of a stuck `in_progress` — and
    # must NOT call end_call (its goodbye audio would only reach a dead socket).
    from koel.voicebot.session.models import CallStatus

    orch = RecordingOrchestrator()
    state = _state("tw_drop")
    ws = FakeTwilioWS([_start("MZ7")])  # no stop frame → abnormal disconnect

    await run_twilio_bridge(ws, orchestrator=orch, state=state)

    assert state.outcome == "call_dropped"
    assert state.status == CallStatus.ended
    assert orch.ended is None  # end_call not invoked on a dead socket


async def test_clean_stop_is_not_overwritten_as_dropped():
    # A clean `stop` ran end_call(outcome="completed"); the abnormal-disconnect
    # guard must not then clobber it — _stopped is True so the finally is a no-op.
    orch = RecordingOrchestrator()
    state = _state("tw_clean")
    ws = FakeTwilioWS([_start("MZ6"), _stop("MZ6")])

    await run_twilio_bridge(ws, orchestrator=orch, state=state)

    assert orch.ended == "completed"
    assert state.outcome == "in_progress"  # end_call is orch-side; bridge left it alone


async def test_duplicate_start_does_not_re_greet():
    calls: list[str] = []
    orch = RecordingOrchestrator()
    ws = FakeTwilioWS([_start("MZ9"), _start("MZ9")])
    bridge = TwilioMediaBridge(websocket=ws, orchestrator=orch, state=_state())

    original_greet = orch.greet

    async def _counting_greet(state, send):  # noqa: ANN001
        calls.append("greet")
        await original_greet(state, send)

    orch.greet = _counting_greet  # type: ignore[method-assign]
    await bridge.run()

    assert calls == ["greet"]  # greeted exactly once despite two start frames
