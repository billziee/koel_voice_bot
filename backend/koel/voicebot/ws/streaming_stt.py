"""Browser (``/ws/calls``) streaming STT for the WS handler (O-6 Wave 2).

An extracted arm of :class:`~koel.voicebot.ws.handler.CallSession`: it keeps the
long-lived Deepgram-session plumbing out of the handler. The session owns one
:class:`BrowserStreamingStt`, pushes each inbound audio frame into it, and closes it
at teardown. A call only routes here when a :data:`StreamingSessionFactory` was
injected (i.e. it resolved to Deepgram *streaming* STT); every non-streaming bot stays
on the handler's unchanged buffered per-utterance transcribe path.

Because it drives one specific call's turn, it holds a back-reference to its owning
``CallSession`` and reads/mutates that session's turn state directly (same ``ws``
package, same lifecycle) — exactly what the equivalent inline handler method would do.

Fully defensive:
  - a connect failure degrades the call to the buffered path (never dropped);
  - a mid-stream send/finish error is swallowed by the underlying session (partials
    just stop) rather than surfacing into the live call;
  - a best-effort partial caption or a turn failure is logged, never raised into the
    session's background reader.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from typing import TYPE_CHECKING

from koel.common.logging import get_logger
from koel.stt.schemas import TranscriptResult
from koel.tts.mixer import pcm16_to_wav
from koel.voicebot.session.models import CallStatus
from koel.voicebot.telephony.ingest import StreamingSessionFactory, StreamingSttSession
from koel.voicebot.ws.protocol import ClientMessage, ServerMessage, ServerMessageType

if TYPE_CHECKING:
    from koel.voicebot.ws.handler import CallSession

logger = get_logger("koel.voicebot.ws.streaming_stt")

# Re-exported so the handler imports its factory type from one place.
__all__ = ["BrowserStreamingStt", "StreamingSessionFactory"]

# Rolling audio window kept alongside the streaming session so the call
# recording still gets the customer's side (Deepgram returns text only) —
# mirrors StreamingTelephonyIngest's own window on the telephony transport.
_MAX_AUDIO_WINDOW_BYTES = 320_000  # 10s at 16 kHz/16-bit mono
_AUDIO_SAMPLE_RATE = 16000  # ClientMessage.sample_rate default / the neural VAD's fixed rate


class BrowserStreamingStt:
    """Feeds inbound browser audio into a long-lived streaming STT session (O-6 Wave 2).

    Interim partials surface as best-effort ``partial_transcript`` captions; utterance
    finals run the SAME downstream the buffered path uses (``handle_final_transcript``
    — records the customer turn + drives the reply). Deepgram does the endpointing, so
    a final flows in identically; the smart-turn / O-5 speculative hold is a
    buffered-path concern delegated to Deepgram here, and barge-in (via
    ``barge_in_candidate``) is unaffected.
    """

    def __init__(
        self, *, owner: CallSession, factory: StreamingSessionFactory | None
    ) -> None:
        self._owner = owner
        self._factory = factory
        self._session_id = owner._state.session_id  # noqa: SLF001 — stable id, for logging
        self._session: StreamingSttSession | None = None
        self._degraded = False
        self._audio_window = bytearray()
        # Serialize utterance finals: unlike the buffered path (single-driver receive
        # loop), finals arrive from the Deepgram reader task(s), so two close finals
        # (or an old session finishing while the next is open) could otherwise drive
        # two overlapping turns → interleaved bot audio + racing session state. Audio
        # keeps flowing to Deepgram (feed() doesn't take this); only turn PROCESSING
        # serializes, matching the buffered path.
        self._final_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """True while the call runs on the streaming session (factory injected + not
        degraded to the buffered path)."""
        return self._factory is not None and not self._degraded

    async def feed(self, msg: ClientMessage) -> bool:
        """Push one inbound audio frame into the long-lived Deepgram session.

        Opens the session lazily on the first frame; a client ``is_final`` VAD-end
        flushes the utterance boundary (``finish``) and drops the session so the next
        utterance opens a fresh one. Returns True when the frame was consumed; False
        when the session could not be opened — the handler then falls back to the
        buffered path for this frame and (via ``enabled``) the rest of the call. Never
        raises: a mid-stream send/finish error is swallowed by the session itself.
        """
        session = self._session or await self._open()
        if session is None:
            return False
        self._session = session
        pcm = self._decode(msg)
        if pcm:
            await session.send_frame(pcm)
            self._audio_window.extend(pcm)
            if len(self._audio_window) > _MAX_AUDIO_WINDOW_BYTES:
                del self._audio_window[: len(self._audio_window) - _MAX_AUDIO_WINDOW_BYTES]
        if msg.is_final:
            await session.finish()
            self._session = None
        return True

    async def aclose(self) -> None:
        """Close the session at teardown (idempotent; no Deepgram socket leak)."""
        session = self._session
        self._session = None
        if session is not None:
            await session.aclose()

    # -- internals ------------------------------------------------------------

    async def _open(self) -> StreamingSttSession | None:
        """Open the streaming session lazily. Returns None (never raises) on a connect
        failure, marking the call degraded so it stays on the buffered path."""
        try:
            # enabled (the handler's guard) proves _factory is not None here.
            session = self._factory(self._forward_partial, self._on_final)  # type: ignore[misc]
            await session.start()
            logger.info(
                "ws_streaming_stt_engaged",
                extra={"extra_fields": {"session_id": self._session_id}},
            )
            return session
        except Exception:  # noqa: BLE001 — connect failure ⇒ degrade to the buffered path
            self._degraded = True
            logger.warning(
                "ws_streaming_stt_start_failed; using buffered path",
                extra={"extra_fields": {"session_id": self._session_id}},
                exc_info=True,
            )
            return None

    async def _forward_partial(self, text: str) -> None:
        """Best-effort live caption of the customer's in-progress utterance (O-6):
        non-authoritative — the turn still runs on the final."""
        if not text.strip():
            return
        try:
            await self._owner.send(
                ServerMessage.of(
                    ServerMessageType.partial_transcript, speaker="customer", text=text
                )
            )
        except Exception:  # noqa: BLE001 — a caption send failure must not kill the reader
            logger.debug("ws_stream_partial_send_failed", exc_info=True)

    async def _on_final(self, stt: TranscriptResult) -> None:
        """A streaming utterance final: run it through the owning session's turn
        pipeline. Best-effort — a turn failure is logged, never raised into the
        session's background reader (which would otherwise be lost)."""
        # Full-conversation call recording (K): snapshot + clear the rolling
        # window regardless of what's below — capture what the customer said
        # even on a blank/dropped result or during an agent hold.
        audio = bytes(self._audio_window)
        self._audio_window.clear()
        if audio:
            try:
                self._owner._buffer_recording_audio(  # noqa: SLF001 — extracted arm of CallSession
                    pcm16_to_wav(audio, _AUDIO_SAMPLE_RATE)
                )
            except Exception:  # noqa: BLE001 — recording is best-effort, never break the reader
                logger.debug("ws_stream_recording_buffer_failed", exc_info=True)
        if not stt.is_final or not stt.text.strip():
            return
        owner = self._owner
        # Serialize with any concurrent utterance final so two turns never overlap.
        async with self._final_lock:
            # On agent hold (3.1) the human operator owns the conversation — drop the
            # customer's streaming turn instead of running the bot loop over the
            # handoff (mirrors _handle_audio's waiting_agent guard; checked inside the
            # lock so a hold that lands mid-wait is still honoured).
            if owner._state.status == CallStatus.waiting_agent:  # noqa: SLF001
                return
            # The customer is taking their turn — any prior bot audio is done.
            owner._bot_speaking = False  # noqa: SLF001 — extracted arm of CallSession
            owner._turn_in_at = time.monotonic()  # noqa: SLF001 — first-audio metric mark
            try:
                await owner._orch.handle_final_transcript(owner._state, owner.send, stt)  # noqa: SLF001
            except Exception:  # noqa: BLE001 — a turn failure degrades; never crash the reader
                logger.warning(
                    "ws_stream_final_turn_failed",
                    extra={"extra_fields": {"session_id": self._session_id}},
                    exc_info=True,
                )
                return
            if owner._state.status == CallStatus.ended:  # noqa: SLF001
                owner._closed = True  # noqa: SLF001 — let the receive loop unwind
            # This turn runs from the Deepgram reader task, NOT the receive loop, so
            # nothing persists the resulting state afterwards. Persist here (and
            # finalize when the turn ended the call) — otherwise a streaming turn
            # that hangs up leaves the receive loop to unwind on the no-input timer,
            # which early-returns on `ended` and exits with the call never recorded.
            await owner._persist_state()  # noqa: SLF001 — extracted arm of CallSession

    @staticmethod
    def _decode(msg: ClientMessage) -> bytes:
        """Raw PCM16 for Deepgram ``linear16`` streaming: prefer the neural-VAD
        ``pcm16_b64`` (raw s16le mono 16 kHz), else ``audio_b64``. A bad base64 frame
        is dropped (never crashes the feed)."""
        b64 = msg.pcm16_b64 or msg.audio_b64 or ""
        if not b64:
            return b""
        try:
            return base64.b64decode(b64)
        except (ValueError, binascii.Error):
            return b""
