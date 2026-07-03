"""WebSocket session handler (PRD §05 / §08 WebSocket Lifecycle Discipline).

Responsibilities:
  - JWT-validated upgrade (rejected with 1008 if unauthenticated).
  - Bind a new session OR resume an existing one within the 15s grace window.
  - Heartbeat ping/pong at 5s intervals to detect dead connections.
  - Drive the no-input timer (5s re-prompt, +5s graceful close).
  - Dispatch client messages: audio chunks, barge-in candidates, end_call.
  - On disconnect: start the Redis grace TTL instead of flushing.
  - On final close: persist the call to PostgreSQL and flush the session.
"""

from __future__ import annotations

import asyncio
import base64
import time

import httpx
from fastapi import WebSocket, WebSocketDisconnect

from koel.common.auth import WS_SUBPROTOCOL
from koel.common.logging import get_logger, set_correlation_id
from koel.common.schemas import Language
from koel.common.telemetry import record_first_audio_latency, span
from koel.stt.schemas import TranscribeRequest
from koel.tts.mixer import concat_wavs
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.persistence import persist_call
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.pipeline.silence import NoInputAction, on_no_input
from koel.voicebot.recording import upload_recording
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.session.store import RedisSessionStore
from koel.voicebot.ws.protocol import (
    ClientMessage,
    ClientMessageType,
    ServerMessage,
    ServerMessageType,
)

logger = get_logger("koel.voicebot.ws")

# Cap the recording buffer so a very long call can't exhaust memory (~a few MB).
_MAX_RECORDING_FRAMES = 500
# Per-chunk cushion (s) added to the playback estimate to absorb browser decode /
# gapless-scheduling lag, so the no-input timer never starts mid-playback.
_CHUNK_PLAYBACK_MARGIN_S = 0.2


class CallSession:
    """Owns one WebSocket connection bound to one SessionState."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        state: SessionState,
        settings: VoiceBotSettings,
        store: RedisSessionStore,
        orchestrator: Orchestrator,
        bargein: BargeInClassifier,
        clients,
        resumed: bool,
        background_tasks: set | None = None,
    ) -> None:
        self._ws = websocket
        self._state = state
        self._settings = settings
        self._store = store
        self._orch = orchestrator
        self._bargein = bargein
        self._clients = clients
        self._resumed = resumed
        # Strong refs to detached finalizer tasks so they aren't GC'd mid-run.
        self._background_tasks = background_tasks if background_tasks is not None else set()
        self._bot_speaking = False
        self._closed = False
        self._finalized = False  # ensures the call is persisted exactly once
        self._disconnect_started = False  # idempotent grace handling
        self._last_pong = time.monotonic()  # heartbeat liveness clock
        self._last_input_at = time.monotonic()  # last *real* client input (no-input timer)
        self._speech_until = 0.0  # monotonic time the bot's current audio finishes playing
        self._send_lock = asyncio.Lock()
        self._audio_buffer: list[bytes] = []  # bot WAV frames for the call recording (K)
        self._turn_in_at: float | None = None  # when the customer's final audio arrived (first-audio metric)

    async def send(self, message: ServerMessage) -> None:
        async with self._send_lock:
            await self._ws.send_text(message.model_dump_json())
        # Barge-in gating: once we dispatch bot audio, the client may still be
        # *playing* it for several seconds — that whole window is when a
        # barge_in_candidate can legitimately arrive. So "bot speaking" stays
        # true from audio dispatch until the customer actually takes their turn
        # (a final audio_chunk) or a confirmed interrupt fires. The transition
        # to the `listening` UI state does NOT clear it (PRD §05).
        if message.type == ServerMessageType.audio_chunk:
            # First bot audio after the customer's final utterance = true
            # audio-in→audio-out first-audio latency vs the §14 budget (Tier 5).
            if self._turn_in_at is not None:
                record_first_audio_latency((time.monotonic() - self._turn_in_at) * 1000)
                self._turn_in_at = None
            self._bot_speaking = True
            # The client plays this audio for duration_ms; the no-input timer
            # must not start counting until playback finishes (PRD §05) — else
            # a 7s greeting would trip the 5s silence timer mid-sentence.
            duration_ms = message.data.get("duration_ms", 0) or 0
            # Accumulate across streamed chunks: if audio is still queued to play,
            # extend from that end, not from now — so the no-input timer waits for
            # ALL queued sentences to finish, not just the last chunk (Tier 2). Real
            # audio gets a small cushion for browser decode/scheduling lag (keeping
            # this an OVER-estimate); the precise end comes from client playback_done.
            extra = duration_ms / 1000 + _CHUNK_PLAYBACK_MARGIN_S if duration_ms > 0 else 0.0
            self._speech_until = max(self._speech_until, time.monotonic()) + extra
            # Buffer bot WAV audio for the call recording (K), capped to bound memory.
            is_wav = message.data.get("audio_format", "wav") == "wav"
            if is_wav and len(self._audio_buffer) < _MAX_RECORDING_FRAMES:
                try:
                    self._audio_buffer.append(base64.b64decode(message.data.get("audio_b64", "")))
                except (ValueError, base64.binascii.Error):
                    pass

    async def run(self) -> None:
        # Echo our subprotocol when the client negotiated one (token-in-subprotocol
        # transport, M); a browser that offered it requires the echo to connect.
        offered = [p.strip() for p in self._ws.headers.get("sec-websocket-protocol", "").split(",")]
        subprotocol = WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None
        await self._ws.accept(subprotocol=subprotocol)
        if self._resumed:
            # PRD §05: emit a RECONNECTED span so resume latency is observable.
            with span(
                "ws.reconnected",
                session_id=self._state.session_id,
                last_turn=self._state.last_confirmed_turn_index,
            ):
                logger.info(
                    "ws_reconnected",
                    extra={"extra_fields": {"session_id": self._state.session_id}},
                )
                await self.send(
                    ServerMessage.of(
                        ServerMessageType.reconnected,
                        text="Sorry, lagta hai line cut ho gayi thi — am I audible?",
                        last_turn=self._state.last_confirmed_turn_index,
                    )
                )
                self._state.status = CallStatus.listening
        else:
            await self._orch.greet(self._state, self.send)
        await self._persist_state()

        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._receive_loop()
        except WebSocketDisconnect:
            await self._on_disconnect()
        finally:
            heartbeat.cancel()

    async def _receive_loop(self) -> None:
        while not self._closed:
            # No-input timer (PRD §05): 5s → re-prompt, a further 5s → close.
            # The window is measured from whichever happened later — the last
            # real client input or the moment the bot's audio finished playing —
            # so the timer only starts once the bot has stopped speaking, and
            # heartbeat pongs never reset it.
            window_ms = (
                self._settings.no_input_reprompt_ms
                if self._state.no_input_strikes == 0
                else self._settings.no_input_close_ms
            )
            baseline = max(self._last_input_at, self._speech_until)
            remaining = (baseline + window_ms / 1000) - time.monotonic()
            if remaining <= 0:
                await self._safe_no_input()
                continue
            try:
                raw = await asyncio.wait_for(self._ws.receive_text(), timeout=remaining)
            except TimeoutError:
                await self._safe_no_input()
                continue

            try:
                msg = ClientMessage.model_validate_json(raw)
            except ValueError:
                await self.send(
                    ServerMessage.of(ServerMessageType.error, code="bad_message", message="Malformed frame.")
                )
                continue

            # Only real customer input resets the no-input clock — not heartbeat
            # pongs, and not playback_done (that's the bot's audio finishing, which
            # is exactly when the no-input window should START).
            if msg.type not in (ClientMessageType.pong, ClientMessageType.playback_done):
                self._last_input_at = time.monotonic()

            # A failure in any AI-service call (httpx) or unexpected error must
            # NOT crash the socket and silently drop the call (PRD §05). Surface
            # an error frame and finalise the call exactly once.
            try:
                await self._dispatch(msg)
                await self._persist_state()
            except WebSocketDisconnect:
                raise  # let run() route this to the grace-TTL path
            except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                await self._fail_call("turn_failed", "A service error interrupted the turn.", exc)

    async def _fail_call(self, code: str, message: str, exc: Exception | None = None) -> None:
        """Best-effort: tell the client, mark the outcome, and persist once."""
        logger.error(
            "turn_failed session=%s code=%s", self._state.session_id, code, exc_info=exc is not None
        )
        if self._state.outcome == "in_progress":
            self._state.outcome = "call_dropped"
        self._state.status = CallStatus.ended
        for frame in (
            ServerMessage.of(ServerMessageType.error, code=code, message=message),
            ServerMessage.of(
                ServerMessageType.call_ended,
                outcome=self._state.outcome,
                escalated=self._state.escalated,
            ),
        ):
            try:
                await self.send(frame)
            except Exception:  # noqa: BLE001 — socket may already be gone
                break
        self._closed = True
        await self._finalize()

    async def _dispatch(self, msg: ClientMessage) -> None:
        if msg.type == ClientMessageType.pong:
            self._last_pong = time.monotonic()  # heartbeat liveness
            return
        if msg.type == ClientMessageType.playback_done:
            # The client's audio queue drained → the bot has truly finished
            # speaking. Start the no-input clock from NOW (precise), overriding the
            # send-time estimate so a re-prompt never fires mid-playback.
            self._speech_until = time.monotonic()
            self._bot_speaking = False
            return
        if msg.type == ClientMessageType.end_call:
            await self._orch.end_call(self._state, self.send, outcome="declined")
            self._closed = True
            return
        if msg.type == ClientMessageType.barge_in_candidate:
            await self._handle_barge_in(msg)
            return
        if msg.type == ClientMessageType.audio_chunk:
            await self._handle_audio(msg)

    async def _handle_audio(self, msg: ClientMessage) -> None:
        stt = await self._clients.transcribe(
            TranscribeRequest(
                session_id=self._state.session_id,
                audio_b64=msg.audio_b64 or "",
                sample_rate=msg.sample_rate,
                is_final=msg.is_final,
                audio_format=msg.audio_format,
                # Pin transcription to the bot's language — otherwise Whisper
                # auto-detects and mislabels idle/short/code-mixed audio (Hindi →
                # Russian/Ukrainian, noise → Spanish/French), garbling the turn.
                language_hint=self._state.language,
            )
        )
        if not stt.is_final:
            # Partial transcripts are only used by the barge-in path.
            return
        # The customer is taking their turn — any prior bot audio is done.
        self._bot_speaking = False
        self._turn_in_at = time.monotonic()  # audio-in mark for the first-audio metric
        await self._orch.handle_final_transcript(self._state, self.send, stt)
        if self._state.status == CallStatus.ended:
            self._closed = True

    async def _handle_barge_in(self, msg: ClientMessage) -> None:
        """Gate 2 (PRD §05): decide whether a VAD candidate is a real interrupt."""
        if not self._bot_speaking:
            return
        partial = msg.data.get("partial_text", "")
        duration = int(msg.data.get("speech_duration_ms", 0))
        lang = self._state.language
        try:
            lang = Language(msg.data.get("language", lang.value))
        except ValueError:
            pass
        # Fold in the bot instance's configured backchannel words (PRD §05).
        extra = {w.lower() for w in self._state.backchannels.get(lang.value, [])}
        decision = self._bargein.evaluate(
            partial_text=partial,
            speech_duration_ms=duration,
            language=lang,
            extra_backchannels=extra or None,
        )
        if decision.interrupt:
            # TTS_KILL: flush playback buffer; mark the bot turn interrupted.
            # Carry the stop-latency budget so the client can target it (PRD §14).
            self._bot_speaking = False
            if self._state.transcript and self._state.transcript[-1].speaker == "bot":
                self._state.transcript[-1].interrupted = True
            await self.send(
                ServerMessage.of(
                    ServerMessageType.tts_kill,
                    reason=decision.reason,
                    stop_target_ms=self._settings.barge_in_stop_target_ms,
                )
            )
        # else: backchannel / too short → keep talking (no message needed)

    async def _safe_no_input(self) -> None:
        """Run the no-input handler; a TTS/CRM failure here must not crash the
        socket and silently drop the call (same never-drop posture as _dispatch)."""
        try:
            await self._handle_no_input()
        except WebSocketDisconnect:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fail_call("no_input_failed", "A service error interrupted the turn.", exc)

    async def _handle_no_input(self) -> None:
        if self._state.status in (CallStatus.ended, CallStatus.escalating):
            return
        outcome = on_no_input(self._state.no_input_strikes, self._state.language)
        # PRD §05: the no-input event MUST be logged on the session.
        logger.info(
            outcome.event,
            extra={
                "extra_fields": {
                    "session_id": self._state.session_id,
                    "event": outcome.event,
                    "strikes": self._state.no_input_strikes,
                }
            },
        )
        if outcome.action == NoInputAction.reprompt:
            self._state.no_input_strikes += 1
            # Sending the re-prompt sets _speech_until to when its audio ends, so
            # the close window starts only after the customer has heard it — not
            # at the same instant as the first window.
            await self._orch._send_bot_turn(  # noqa: SLF001 — internal helper reuse
                self._state, self.send, text=outcome.text, tone=self._state.default_tone
            )
        else:
            self._state.outcome = "customer_unresponsive"
            await self._orch._send_bot_turn(  # noqa: SLF001
                self._state, self.send, text=outcome.text, tone=self._state.default_tone
            )
            if self._state.customer_id:
                await self._clients.flag_customer(self._state.customer_id, "retry", "no input")
            await self._orch.end_call(self._state, self.send, outcome="customer_unresponsive")
            self._closed = True
        await self._persist_state()

    async def _heartbeat_loop(self) -> None:
        """Ping every 5s AND detect a dead peer (PRD §08): if no pong arrives
        within the timeout window, close the socket so the receive loop routes
        the call onto the 15s grace path."""
        interval = self._settings.heartbeat_interval_seconds
        timeout = self._settings.heartbeat_timeout_seconds
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                await self.send(ServerMessage.of(ServerMessageType.ping))
                if time.monotonic() - self._last_pong > timeout:
                    logger.warning(
                        "ws_heartbeat_timeout",
                        extra={"extra_fields": {"session_id": self._state.session_id}},
                    )
                    try:
                        await self._ws.close(code=1001, reason="heartbeat timeout")
                    except Exception:  # noqa: BLE001 — socket may already be gone
                        pass
                    return  # receive loop will raise → _on_disconnect grace path
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError, Exception):  # noqa: BLE001
            return  # socket gone / closing — heartbeat is best-effort

    async def _on_disconnect(self) -> None:
        """Socket dropped (PRD §05): freeze state on the 15s grace TTL, don't flush.

        Also arms a detached finalizer: if no reconnect re-binds the session
        within the grace window, the call is recorded as ``call_dropped`` with
        its partial transcript persisted and the CRM flagged for a callback.
        """
        if self._disconnect_started:
            return
        self._disconnect_started = True
        if self._state.status == CallStatus.ended:
            await self._finalize()
            return
        self._state.status = CallStatus.reconnecting
        await self._store.save(self._state)
        alive = await self._store.begin_grace(self._state.session_id)
        logger.info(
            "ws_disconnected_grace_started",
            extra={"extra_fields": {"session_id": self._state.session_id, "grace_alive": alive}},
        )
        task = asyncio.create_task(self._await_grace_then_finalize())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _await_grace_then_finalize(self) -> None:
        """Fire after the grace window. If no reconnect re-bound the session,
        finalise it as a dropped call (PRD §05 no-reconnect branch)."""
        await asyncio.sleep(self._settings.session_grace_ttl_seconds)
        if self._finalized:
            return
        # A reconnect would have moved status off `reconnecting`; if so, that
        # newer session owns the call and we must not touch it.
        current = await self._store.load(self._state.session_id)
        if current is not None and current.status != CallStatus.reconnecting:
            return
        state = current or self._state  # Redis copy if alive, else in-memory
        if state.outcome == "in_progress":
            state.outcome = "call_dropped"
        state.status = CallStatus.ended
        self._state = state
        logger.info(
            "call_dropped_no_reconnect",
            extra={"extra_fields": {"session_id": state.session_id}},
        )
        if state.customer_id:
            try:
                await self._clients.flag_customer(
                    state.customer_id, "callback", "call dropped mid-conversation"
                )
            except Exception:  # noqa: BLE001 — CRM flag is best-effort
                logger.warning("dropped_call_crm_flag_failed session=%s", state.session_id)
        await self._finalize()

    async def _finalize(self) -> None:
        # Exactly-once: reachable from _persist_state, _on_disconnect, and _fail_call.
        if self._finalized:
            return
        self._finalized = True
        await self._upload_recording()
        call_id = await persist_call(self._state)
        if call_id is not None:
            # Only flush Redis once the call is safely in PostgreSQL; otherwise
            # leave it on TTL so the state isn't lost on a transient DB failure.
            await self._store.delete(self._state.session_id)
        else:
            self._finalized = False  # allow a later attempt to persist

    async def _upload_recording(self) -> None:
        """Mux the buffered bot audio and upload it (K), then point recording_url at
        the real object — or NULL it out when recording is disabled, so we never
        persist a URL that resolves to nothing (audit finding)."""
        wav = concat_wavs(self._audio_buffer) if self._audio_buffer else b""
        url = await upload_recording(self._state.session_id, wav) if wav else None
        # Persist only a URL that actually has bytes behind it (or None).
        self._state.recording_url = url

    async def _persist_state(self) -> None:
        await self._store.save(self._state)
        if self._state.status == CallStatus.ended:
            await self._finalize()


async def handle_connection(
    websocket: WebSocket,
    *,
    session_id: str,
    state: SessionState,
    settings: VoiceBotSettings,
    store: RedisSessionStore,
    orchestrator: Orchestrator,
    bargein: BargeInClassifier,
    clients,
    resumed: bool,
    background_tasks: set | None = None,
) -> None:
    set_correlation_id(session_id)
    call = CallSession(
        websocket=websocket,
        state=state,
        settings=settings,
        store=store,
        orchestrator=orchestrator,
        bargein=bargein,
        clients=clients,
        resumed=resumed,
        background_tasks=background_tasks,
    )
    await call.run()
