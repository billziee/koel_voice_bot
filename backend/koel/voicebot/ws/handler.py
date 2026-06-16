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

import httpx
from fastapi import WebSocket, WebSocketDisconnect

from koel.common.logging import get_logger, set_correlation_id
from koel.common.schemas import Language
from koel.stt.schemas import TranscribeRequest
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.persistence import persist_call
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.pipeline.silence import NoInputAction, on_no_input
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.session.store import RedisSessionStore
from koel.voicebot.ws.protocol import (
    ClientMessage,
    ClientMessageType,
    ServerMessage,
    ServerMessageType,
)

logger = get_logger("koel.voicebot.ws")


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
    ) -> None:
        self._ws = websocket
        self._state = state
        self._settings = settings
        self._store = store
        self._orch = orchestrator
        self._bargein = bargein
        self._clients = clients
        self._resumed = resumed
        self._bot_speaking = False
        self._closed = False
        self._finalized = False  # ensures the call is persisted exactly once
        self._send_lock = asyncio.Lock()

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
            self._bot_speaking = True

    async def run(self) -> None:
        await self._ws.accept()
        if self._resumed:
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
            try:
                # No-input timer (PRD §05): re-prompt / close on silence.
                raw = await asyncio.wait_for(
                    self._ws.receive_text(),
                    timeout=self._settings.no_input_reprompt_ms / 1000,
                )
            except TimeoutError:
                await self._handle_no_input()
                continue

            try:
                msg = ClientMessage.model_validate_json(raw)
            except ValueError:
                await self.send(
                    ServerMessage.of(ServerMessageType.error, code="bad_message", message="Malformed frame.")
                )
                continue

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
            )
        )
        if not stt.is_final:
            # Partial transcripts are only used by the barge-in path.
            return
        # The customer is taking their turn — any prior bot audio is done.
        self._bot_speaking = False
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
        decision = self._bargein.evaluate(
            partial_text=partial, speech_duration_ms=duration, language=lang
        )
        if decision.interrupt:
            # TTS_KILL: flush playback buffer; mark the bot turn interrupted.
            self._bot_speaking = False
            if self._state.transcript and self._state.transcript[-1].speaker == "bot":
                self._state.transcript[-1].interrupted = True
            await self.send(ServerMessage.of(ServerMessageType.tts_kill, reason=decision.reason))
        # else: backchannel / too short → keep talking (no message needed)

    async def _handle_no_input(self) -> None:
        if self._state.status in (CallStatus.ended, CallStatus.escalating):
            return
        outcome = on_no_input(self._state.no_input_strikes, self._state.language)
        if outcome.action == NoInputAction.reprompt:
            self._state.no_input_strikes += 1
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
        interval = self._settings.heartbeat_interval_seconds
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                await self.send(ServerMessage.of(ServerMessageType.ping))
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError, Exception):  # noqa: BLE001
            return  # socket gone / closing — heartbeat is best-effort

    async def _on_disconnect(self) -> None:
        """Socket dropped (PRD §05): freeze state on the 15s grace TTL, don't flush."""
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

    async def _finalize(self) -> None:
        # Exactly-once: reachable from _persist_state, _on_disconnect, and _fail_call.
        if self._finalized:
            return
        self._finalized = True
        call_id = await persist_call(self._state)
        if call_id is not None:
            # Only flush Redis once the call is safely in PostgreSQL; otherwise
            # leave it on TTL so the state isn't lost on a transient DB failure.
            await self._store.delete(self._state.session_id)
        else:
            self._finalized = False  # allow a later attempt to persist

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
    )
    await call.run()
