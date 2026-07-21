"""Twilio Media Streams ↔ orchestrator bridge (roadmap 1.1 + 1.3).

The orchestrator is transport-agnostic: it drives a call purely through a
``send`` callback plus an audio-in path, and neither knows nor cares whether the
audio comes from a browser WebSocket (``ws/handler.py``) or a phone carrier. This
module is the *phone-transport* analogue of that browser handler — it adapts a
Twilio Media Streams WebSocket to those same seams and reuses the orchestrator
unchanged:

  * outbound: orchestrator ``ServerMessage``s → Twilio frames — a synthesized
    ``audio_chunk``/``filler`` becomes a ``media`` frame (PCM16→8 kHz µ-law), and
    a confirmed barge-in (``tts_kill``) becomes a ``clear`` frame that flushes
    the carrier's playout buffer immediately (PRD §05);
  * inbound: Twilio frames → orchestrator — a ``media`` frame's 8 kHz µ-law is
    decoded to PCM16 and handed to the injected audio hook (the STT seam); a
    ``dtmf`` frame is mapped to a synthetic customer turn (1.3); ``start`` opens
    the call with the greeting and ``stop`` ends it.

It is *transport mechanics only* — it never reimplements orchestrator logic —
and it is defensive: a malformed carrier frame is logged and skipped, never
allowed to crash the live call.

Testability: the WebSocket is injected as a small structural :class:`TwilioWebSocket`
protocol (``iter_text`` / ``send_text``), so a fake WS can drive the bridge with
scripted JSON frames and no carrier. Likewise the orchestrator is accepted as the
:class:`OrchestratorLike` protocol and the STT audio sink as a plain callback, so
the whole adapter unit-tests without Twilio, STT, or Redis.

The live ``/twilio/stream`` WebSocket route that constructs a session + orchestrator
and hands the socket here is the remaining wiring step (dormant until a Twilio
account is configured).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol

from koel.common.logging import get_logger
from koel.stt.schemas import TranscriptResult
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.telephony.codec import pcm16_to_ulaw8k, ulaw8k_to_pcm16
from koel.voicebot.telephony.dtmf import dtmf_to_turn
from koel.voicebot.telephony.twilio import (
    TwilioEvent,
    parse_twilio_message,
    twilio_clear_frame,
    twilio_media_frame,
)
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

logger = get_logger("koel.voicebot.telephony.bridge")

# Identical to the orchestrator's own alias — the callback it emits turns through.
Sender = Callable[[ServerMessage], Awaitable[None]]

# STT seam: decoded inbound PCM16 + its sample rate. The production wiring points
# this at the streaming-STT/endpointing pipeline that ultimately calls
# ``orchestrator.handle_final_transcript``; tests point it at a recorder.
CallerAudioHook = Callable[[bytes, int], Awaitable[None]]

# STT ingests 16 kHz PCM16 (matches ``ulaw8k_to_pcm16``'s default target); Twilio
# always sends 8 kHz µ-law media.
_STT_SAMPLE_RATE = 16000

# Outbound audio the bridge already receives pre-transcoded as raw 8 kHz µ-law
# (SynthesizeRequest.telephony=True path) carries this format tag.
_ULAW_FORMAT = "ulaw"

# Twilio Media Streams sends a frame (media/mark/etc) roughly every 20ms for the
# life of a real call — the carrier, not silence on the line, drives the cadence
# (comfort-noise media keeps flowing even when the caller says nothing). Total
# silence on the socket for this long means the TCP connection wedged without a
# clean close (NAT/firewall drop, half-open socket) rather than a quiet caller —
# the browser transport catches the equivalent failure via its heartbeat
# timeout; Media Streams has no ping/pong of its own, so this read timeout is
# the telephony analogue.
_DEFAULT_FRAME_IDLE_TIMEOUT_S = 30.0


class TwilioWebSocket(Protocol):
    """Minimal structural contract for the injected socket.

    Satisfied by Starlette/FastAPI's ``WebSocket`` and by any test fake. The
    route is expected to have ``accept()``ed the socket before handing it here;
    the bridge only reads frames and writes frames.
    """

    async def send_text(self, data: str) -> None: ...

    def iter_text(self) -> AsyncIterator[str]: ...


class OrchestratorLike(Protocol):
    """The orchestrator surface the bridge drives — structurally satisfied by
    :class:`koel.voicebot.pipeline.orchestrator.Orchestrator` (never modified)."""

    async def greet(self, state: SessionState, send: Sender) -> None: ...

    async def handle_final_transcript(
        self, state: SessionState, send: Sender, stt: TranscriptResult
    ) -> None: ...

    async def end_call(self, state: SessionState, send: Sender, *, outcome: str) -> None: ...


def _wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """(raw PCM16 samples, sample_rate) from a mono 16-bit PCM WAV clip.

    Validates the format the µ-law transcoder assumes — a stereo or non-16-bit clip
    would be silently mis-transcoded into garbled carrier audio. A mismatch raises
    ValueError, which the caller (_play) already catches and drops + logs.
    """
    with wave.open(io.BytesIO(data), "rb") as wav:
        channels, width = wav.getnchannels(), wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if channels != 1 or width != 2:
        raise ValueError(f"expected mono 16-bit PCM WAV, got {channels}ch/{width * 8}-bit")
    return frames, rate


class TwilioMediaBridge:
    """Bridges one Twilio Media Streams WebSocket to one orchestrator-driven call.

    Construct with the (already-accepted) socket, the orchestrator, the seeded
    :class:`SessionState`, the bot's ``dtmf_map`` and an inbound-audio hook, then
    ``await run()`` — it consumes carrier frames until the stream stops or the
    socket drops.

    Also structurally satisfies :class:`koel.voicebot.telephony.carrier.CarrierBridge`
    (``send_audio``/``send_clear``/``hangup``) — the carrier-agnostic surface a
    future Exotel adapter is meant to match too.
    """

    def __init__(
        self,
        *,
        websocket: TwilioWebSocket,
        orchestrator: OrchestratorLike,
        state: SessionState,
        dtmf_map: Mapping[str, str] | None = None,
        on_caller_audio: CallerAudioHook | None = None,
        inbound_sample_rate: int = _STT_SAMPLE_RATE,
        on_started: Callable[[], Awaitable[None]] | None = None,
        on_outbound: Callable[[ServerMessage], Awaitable[None]] | None = None,
        frame_idle_timeout_s: float = _DEFAULT_FRAME_IDLE_TIMEOUT_S,
    ) -> None:
        self._ws = websocket
        self._orch = orchestrator
        self._state = state
        self._dtmf_map: Mapping[str, str] = dtmf_map or {}
        self._on_caller_audio = on_caller_audio
        self._inbound_sample_rate = inbound_sample_rate
        # Fired once the Media Streams `start` frame lands and the CallSid is
        # captured onto `state` — the route's hook to persist it to Redis
        # immediately (previously nothing did, so a fresh SessionState.load()
        # elsewhere — e.g. the operator warm-transfer endpoint — always saw
        # twilio_call_sid=None for a real call).
        self._on_started = on_started
        # Observer fired on EVERY dispatched ServerMessage, regardless of
        # whether it came from the route's own turn-handling or from this
        # bridge's own greet()/DTMF calls into the orchestrator — the seam the
        # call session hooks cross-cutting bookkeeping onto (first-audio
        # latency, the no-input clock, the recording buffer, the live monitor
        # tee, and detecting `call_ended` to actually terminate the PSTN leg).
        # Best-effort: an observer failure must never break the live call.
        self._on_outbound = on_outbound
        self._frame_idle_timeout_s = frame_idle_timeout_s
        # ``streamSid`` scopes every outbound frame; learned from the first frame
        # that carries it (``start``, and echoed on later frames).
        self._stream_sid: str | None = None
        self._started = False  # guard against a duplicate ``start`` re-greeting
        self._stopped = False
        # Per-direction audioop.ratecv continuation state — this instance is
        # one bridge per call, so carrying it for the instance's lifetime keeps
        # the resample filter continuous across the many small chunks/frames a
        # real call is made of (see codec.py's docstrings for why that matters).
        self._outbound_resample_state = None
        self._inbound_resample_state = None
        # Barge-in Gate 2 needs to know whether the bot is currently talking —
        # the browser transport tracks this on CallSession (client-driven VAD
        # candidates); Twilio has no such client, so the bridge itself tracks
        # it from the outbound frames it's already dispatching.
        self._bot_speaking = False

    @property
    def is_bot_speaking(self) -> bool:
        return self._bot_speaking

    @property
    def stopped(self) -> bool:
        """Whether Twilio already knows this call ended (a clean `stop` frame
        or a prior :meth:`hangup`) — the discriminator the call session uses
        to decide whether an in-turn `call_ended` still needs a REST hangup."""
        return self._stopped

    def mark_customer_turn(self) -> None:
        """Call once the customer's utterance has been handed to the
        orchestrator (a final transcript) — the bot is no longer the one
        talking, mirroring CallSession's ``_bot_speaking = False`` on the
        browser path."""
        self._bot_speaking = False

    # -- outbound: orchestrator ServerMessage → Twilio frame -----------------

    async def send(self, message: ServerMessage) -> None:
        """The orchestrator's ``send`` callback, translating to Twilio frames.

        Only audio-bearing and flush messages have a carrier-wire equivalent;
        FE-only frames (``call_state``/``transcript``/``ping``/…) are ignored.
        """
        if message.type in (ServerMessageType.audio_chunk, ServerMessageType.filler):
            self._bot_speaking = True
            await self._play(message.data)
        elif message.type == ServerMessageType.tts_kill:
            await self.send_clear()
        # Other message types have no Media Streams representation on the wire
        # but may still matter to the call session's observer below.
        if self._on_outbound is not None:
            try:
                await self._on_outbound(message)
            except Exception:  # noqa: BLE001 — bookkeeping must never break the live call
                logger.warning(
                    "twilio_outbound_hook_failed",
                    extra={"extra_fields": {"session_id": self._state.session_id}},
                    exc_info=True,
                )

    async def send_audio(self, pcm16: bytes, sample_rate: int) -> None:
        """:class:`CarrierBridge` entry point — play synthesized PCM to the
        caller, transcoding to 8 kHz µ-law for Twilio.

        Shares the same instance-scoped resample state as the
        ``ServerMessage``-driven :meth:`_play` path, so a caller mixing both
        entry points still transcodes as one continuous stream, not a fresh
        filter per call.
        """
        if self._stream_sid is None:
            return
        self._bot_speaking = True
        ulaw, self._outbound_resample_state = pcm16_to_ulaw8k(
            pcm16, sample_rate, self._outbound_resample_state
        )
        if ulaw:
            await self._ws.send_text(twilio_media_frame(self._stream_sid, ulaw))

    async def _play(self, data: dict) -> None:
        audio_b64 = data.get("audio_b64")
        if not audio_b64 or self._stream_sid is None:
            return
        audio_format = str(data.get("audio_format", "wav"))
        try:
            ulaw = self._to_ulaw(str(audio_b64), audio_format)
        except (ValueError, binascii.Error, wave.Error, EOFError):
            logger.warning(
                "twilio_outbound_transcode_failed",
                extra={"extra_fields": {"session_id": self._state.session_id, "fmt": audio_format}},
                exc_info=True,
            )
            return
        if ulaw:
            await self._ws.send_text(twilio_media_frame(self._stream_sid, ulaw))

    async def send_clear(self) -> None:
        """:class:`CarrierBridge` entry point — flush audio the carrier has
        buffered but not yet played. A confirmed barge-in must silence the
        bot immediately (PRD §05)."""
        self._bot_speaking = False
        if self._stream_sid is None:
            return
        await self._ws.send_text(twilio_clear_frame(self._stream_sid))

    def _to_ulaw(self, audio_b64: str, audio_format: str) -> bytes:
        """Bot audio → raw 8 kHz µ-law for a Twilio media payload.

        Audio synthesized in telephony mode is already µ-law (passed through);
        otherwise it is a PCM16 WAV that is decoded and transcoded. Resample
        state is threaded across calls (instance-scoped) so a multi-chunk
        streamed utterance transcodes as one continuous stream, not one fresh
        filter per chunk.
        """
        raw = base64.b64decode(audio_b64)
        if audio_format == _ULAW_FORMAT:
            return raw
        pcm, rate = _wav_to_pcm16(raw)
        ulaw, self._outbound_resample_state = pcm16_to_ulaw8k(
            pcm, rate, self._outbound_resample_state
        )
        return ulaw

    # -- inbound: Twilio frame → orchestrator --------------------------------

    async def run(self) -> None:
        """Consume inbound frames until the stream stops or the socket drops.

        Each frame is handled in isolation: a malformed or unexpected frame is
        logged and skipped so one bad frame never tears down a live call.
        """
        logger.info(
            "twilio_bridge_open", extra={"extra_fields": {"session_id": self._state.session_id}}
        )
        try:
            frames = self._ws.iter_text()
            while True:
                try:
                    raw = await asyncio.wait_for(
                        frames.__anext__(), timeout=self._frame_idle_timeout_s
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    # No carrier frame at all (not even comfort-noise media) for the
                    # whole window — a wedged socket, not a quiet caller. Fall through
                    # to the same abnormal-disconnect handling a dropped socket gets.
                    logger.warning(
                        "twilio_frame_idle_timeout",
                        extra={"extra_fields": {
                            "session_id": self._state.session_id,
                            "timeout_s": self._frame_idle_timeout_s,
                        }},
                    )
                    break
                try:
                    await self._on_frame(raw)
                except Exception:  # noqa: BLE001 — a single frame must never drop the call
                    logger.warning(
                        "twilio_frame_handling_failed",
                        extra={"extra_fields": {"session_id": self._state.session_id}},
                        exc_info=True,
                    )
                if self._stopped:
                    break
        finally:
            # Abnormal disconnect: the carrier socket dropped without a Twilio
            # `stop` frame (a clean stop already ran end_call via _on_stop). Mark
            # the call dropped so the owning route persists the true outcome rather
            # than a stuck `in_progress` — mirrors the browser handler's grace path.
            # We don't call end_call here: its goodbye audio would only go to a dead
            # socket, and this adapter owns transport, not persistence.
            if not self._stopped and self._state.outcome == "in_progress":
                self._state.outcome = "call_dropped"
                self._state.status = CallStatus.ended
            logger.info(
                "twilio_bridge_closed",
                extra={"extra_fields": {
                    "session_id": self._state.session_id,
                    "outcome": self._state.outcome,
                }},
            )

    async def _on_frame(self, raw: str) -> None:
        try:
            event = parse_twilio_message(raw)
        except Exception:  # noqa: BLE001 — bad JSON / bad base64 / wrong shape → skip
            logger.warning(
                "twilio_frame_parse_failed",
                extra={"extra_fields": {"session_id": self._state.session_id}},
            )
            return

        # Learn/refresh the stream sid from whatever frame carries it.
        if event.stream_sid:
            self._stream_sid = event.stream_sid

        if event.event == "start":
            await self._on_start(event)
        elif event.event == "media":
            await self._on_media(event)
        elif event.event == "dtmf":
            await self._on_dtmf(event)
        elif event.event == "stop":
            await self._on_stop()
        elif event.event in ("connected", "mark"):
            return  # protocol handshake / playback ack — nothing to do
        else:
            logger.debug(
                "twilio_event_ignored",
                extra={"extra_fields": {
                    "session_id": self._state.session_id, "event": event.event,
                }},
            )

    async def _on_start(self, event: TwilioEvent) -> None:
        if self._started:
            return  # duplicate start — do not re-greet
        self._started = True
        # The underlying PSTN leg's CallSid — needed to REST-control the live
        # call for a warm transfer (3.1 Phase B); harmless to store even when
        # that feature is never used.
        if event.call_sid:
            self._state.twilio_call_sid = event.call_sid
            if self._on_started is not None:
                await self._on_started()
        # Opening turn — same entry point the browser handler uses for a fresh call.
        await self._orch.greet(self._state, self.send)

    async def _on_media(self, event: TwilioEvent) -> None:
        ulaw = event.payload_ulaw
        if not ulaw or self._on_caller_audio is None:
            return
        # Resample state threaded across every 20ms frame — see codec.py.
        pcm16, self._inbound_resample_state = ulaw8k_to_pcm16(
            ulaw, target_rate=self._inbound_sample_rate, resample_state=self._inbound_resample_state
        )
        await self._on_caller_audio(pcm16, self._inbound_sample_rate)

    async def _on_dtmf(self, event: TwilioEvent) -> None:
        """A pressed key → the utterance it stands for → a synthetic customer
        turn through the normal orchestrator path (1.3). Unmapped digit → ignored."""
        digit = event.digit or ""
        utterance = dtmf_to_turn(digit, self._dtmf_map)
        if utterance is None:
            logger.info(
                "dtmf_unmapped",
                extra={"extra_fields": {"session_id": self._state.session_id, "digit": digit}},
            )
            return
        stt = TranscriptResult(
            session_id=self._state.session_id,
            text=utterance,
            is_final=True,
            # High-confidence, in-language, zero-duration: a keypress can never be
            # mis-flagged by the orchestrator's unsupported-language heuristic.
            language_id=self._state.language,
            language_confidence=1.0,
            word_confidence=1.0,
            speech_duration_ms=0,
        )
        await self._orch.handle_final_transcript(self._state, self.send, stt)

    async def _on_stop(self) -> None:
        self._stopped = True
        await self._orch.end_call(self._state, self.send, outcome="completed")

    async def hangup(self) -> None:
        """:class:`CarrierBridge` entry point — end the PSTN leg.

        Mirrors :meth:`_on_stop`'s clean-stop path (same ``completed`` outcome
        through the same orchestrator call) so a programmatically-triggered
        hangup persists identically to a carrier-initiated ``stop`` frame.
        A no-op if the call already ended, carrier-initiated or otherwise.
        """
        if self._stopped:
            return
        self._stopped = True
        await self._orch.end_call(self._state, self.send, outcome="completed")


async def run_twilio_bridge(
    websocket: TwilioWebSocket,
    *,
    orchestrator: OrchestratorLike,
    state: SessionState,
    dtmf_map: Mapping[str, str] | None = None,
    on_caller_audio: CallerAudioHook | None = None,
    inbound_sample_rate: int = _STT_SAMPLE_RATE,
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_outbound: Callable[[ServerMessage], Awaitable[None]] | None = None,
    frame_idle_timeout_s: float = _DEFAULT_FRAME_IDLE_TIMEOUT_S,
) -> None:
    """Construct a :class:`TwilioMediaBridge` and run it to completion."""
    bridge = TwilioMediaBridge(
        websocket=websocket,
        orchestrator=orchestrator,
        state=state,
        dtmf_map=dtmf_map,
        on_caller_audio=on_caller_audio,
        inbound_sample_rate=inbound_sample_rate,
        on_started=on_started,
        on_outbound=on_outbound,
        frame_idle_timeout_s=frame_idle_timeout_s,
    )
    await bridge.run()
