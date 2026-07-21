"""Telephony call session (roadmap 1.1/1.3/3.1/O-5/O-6) — the phone-transport
analogue of ``ws/handler.CallSession``.

The bridge (``telephony/bridge.py``) and the ingest classes (``telephony/
ingest.py``) are pure transport/endpointing mechanics; the orchestrator itself
is transport-agnostic. This class is what was missing between them: the
per-call state and turn-handling policy a real phone call needs, so it stops
being a thinner, gap-riddled cousin of the browser call and starts being an
equivalent one — smart-turn mid-thought holds + speculative RAG, human-handoff
registration (so the warm-transfer feature has a live call to attach to) with
actual gating while on hold, a no-input watchdog, first-audio latency, a call
recording buffer, live-monitor visibility, and end-of-call persistence, all of
which the browser transport already had and telephony, audited against it,
did not.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time

from koel.common.logging import get_logger, set_correlation_id
from koel.common.telemetry import record_first_audio_latency, span
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.tts.mixer import concat_wavs, pcm16_to_wav
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.escalation import EscalationHub
from koel.voicebot.monitor import LiveMonitor
from koel.voicebot.persistence import abandon_escalation_by_session, create_escalation, persist_call
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.endpointing import SmartTurnAnalyzer, merge_transcripts
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.pipeline.postcall import dispatch_followup, summarize_and_annotate_crm
from koel.voicebot.pipeline.silence import NoInputAction, on_no_input
from koel.voicebot.pipeline.specrag import SpeculativeRagCoordinator
from koel.voicebot.pipeline.warm_transfer import end_twilio_call
from koel.voicebot.recording import upload_recording
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.session.store import RedisSessionStore
from koel.voicebot.telephony.bargein_gate import TelephonyBargeInGate
from koel.voicebot.telephony.bridge import TwilioMediaBridge
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

logger = get_logger("koel.voicebot.telephony.session")

# Mirrors ws/handler.py's own cap — bounds recording-buffer memory on a long call.
_MAX_RECORDING_FRAMES = 500
# Cushion added to a chunk's declared duration_ms so the no-input clock's
# playback-finish estimate stays an over-estimate (matches ws/handler.py).
_CHUNK_PLAYBACK_MARGIN_S = 0.2
# How often the no-input watchdog re-checks the clock. Coarser than the
# browser's precise single-shot timeout (which blocks on the next socket
# frame) since telephony has no equivalent single "next event" to wait on —
# a 1s poll is precise enough against an 8s window.
_NO_INPUT_POLL_INTERVAL_S = 1.0
# Both ingest paths hand on_utterance PCM16 at this rate (the bridge decodes
# Twilio's 8 kHz µ-law up to it before feeding either one) — see
# _TELEPHONY_STT_SAMPLE_RATE in main.py / _STT_SAMPLE_RATE in bridge.py.
_TELEPHONY_AUDIO_SAMPLE_RATE = 16000


def _pcm16_to_wav_b64(pcm16: bytes, sample_rate: int) -> str:
    """Wrap raw PCM16 mono samples in a WAV container (base64) for STT_svc."""
    return base64.b64encode(pcm16_to_wav(pcm16, sample_rate)).decode()


class TelephonyCallSession:
    """Owns one PSTN call's turn-handling state. Construct, then :meth:`bind`
    the bridge + STT ingest built from this session's own callback methods,
    then :meth:`run`."""

    def __init__(
        self,
        *,
        state: SessionState,
        settings: VoiceBotSettings,
        store: RedisSessionStore,
        orchestrator: Orchestrator,
        bargein: BargeInClassifier,
        clients,
        smart_turn: SmartTurnAnalyzer | None,
        escalations: EscalationHub,
        monitor: LiveMonitor,
        background_tasks: set[asyncio.Task],
    ) -> None:
        self._state = state
        self._settings = settings
        self._store = store
        self._orch = orchestrator
        self._clients = clients
        self._smart_turn = smart_turn
        self._escalations = escalations
        self._monitor = monitor
        self._background_tasks = background_tasks

        self._partial_gate = TelephonyBargeInGate(bargein)
        self._spec = SpeculativeRagCoordinator(
            settings=settings, state=state, clients=clients, orchestrator=orchestrator
        )
        # Bound via bind(), before run() — mirrors main.py's prior "declare
        # bridge, build ingest against its methods, construct bridge" ordering.
        self._bridge: TwilioMediaBridge | None = None
        self._ingest = None

        self._escalation_id: str | None = None
        self._pending_turn: tuple[TranscriptResult, asyncio.Task] | None = None
        self._turn_in_at: float | None = None  # audio-in mark for the first-audio metric
        self._speech_until = 0.0  # estimated monotonic time the bot's queued audio finishes
        self._last_input_at = time.monotonic()  # last real customer input (no-input clock)
        self._audio_buffer: list[bytes] = []  # bot WAV frames for the call recording
        self._no_input_task: asyncio.Task | None = None

    def bind(self, *, bridge: TwilioMediaBridge, ingest) -> None:  # noqa: ANN001
        self._bridge = bridge
        self._ingest = ingest

    # -- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        assert self._bridge is not None and self._ingest is not None  # bind() runs first
        set_correlation_id(self._state.session_id)
        await self._monitor.open(self._state.session_id, self._state.bot_id, self._state.org_id)
        self._last_input_at = time.monotonic()
        self._no_input_task = asyncio.create_task(self._no_input_watchdog())
        try:
            with span("telephony.call", session_id=self._state.session_id):
                await self._bridge.run()
        finally:
            if self._no_input_task is not None:
                self._no_input_task.cancel()
                await asyncio.gather(self._no_input_task, return_exceptions=True)
            if self._pending_turn is not None:
                _, timer = self._pending_turn
                self._pending_turn = None
                timer.cancel()
            self._spec.cancel()
            # Drain any speech still buffered when the stream stopped — unless
            # the call already ended normally (a stop/hangup already ran
            # end_call), in which case there's no one left to answer.
            if self._state.status != CallStatus.ended:
                await self._ingest.flush()
            aclose = getattr(self._ingest, "aclose", None)
            if aclose is not None:
                await aclose()
            await self._finalize()

    async def _finalize(self) -> None:
        await self._monitor.close(self._state.session_id)
        if self._escalation_id is not None:
            self._escalations.unregister(self._state.session_id)
            await abandon_escalation_by_session(self._state.session_id)
        wav = concat_wavs(self._audio_buffer) if self._audio_buffer else b""
        url = await upload_recording(self._state.session_id, wav) if wav else None
        self._state.recording_url = url
        call_id = await persist_call(self._state)
        if call_id is None:
            return  # persistence failure — Redis still holds the state for a later attempt
        await self._store.delete(self._state.session_id)
        dispatch_followup(self._state, self._background_tasks)
        summarize_and_annotate_crm(
            self._state, clients=self._clients, settings=self._settings,
            background_tasks=self._background_tasks,
        )

    # -- bridge hooks -------------------------------------------------------

    async def on_call_started(self) -> None:
        """Bridge's on_started hook: the CallSid just landed on `state` — save
        it to Redis immediately so a fresh load() (the operator warm-transfer
        endpoint) actually sees it instead of a stale None."""
        await self._store.save(self._state)

    async def on_outbound(self, message: ServerMessage) -> None:
        """Bridge's on_outbound hook: fires for every dispatched ServerMessage
        regardless of entry point (greet, DTMF turn, or a normal STT turn) —
        the single seam for cross-cutting bookkeeping the browser transport
        gets for free inside CallSession.send()."""
        if message.type == ServerMessageType.transcript:
            await self._monitor.record_turn(
                self._state.session_id,
                str(message.data.get("speaker", "")),
                str(message.data.get("text", "")),
            )
        elif message.type == ServerMessageType.call_state:
            await self._monitor.set_status(
                self._state.session_id, str(message.data.get("status", ""))
            )
        elif message.type == ServerMessageType.audio_chunk:
            if self._turn_in_at is not None:
                record_first_audio_latency((time.monotonic() - self._turn_in_at) * 1000)
                self._turn_in_at = None
            duration_ms = message.data.get("duration_ms", 0) or 0
            extra = duration_ms / 1000 + _CHUNK_PLAYBACK_MARGIN_S if duration_ms > 0 else 0.0
            self._speech_until = max(self._speech_until, time.monotonic()) + extra
            is_wav = message.data.get("audio_format", "wav") == "wav"
            if is_wav:
                try:
                    self._buffer_recording_audio(base64.b64decode(message.data.get("audio_b64", "")))
                except (ValueError, binascii.Error):
                    pass
        elif message.type == ServerMessageType.call_ended:
            await self._end_call_leg()

    def _buffer_recording_audio(self, wav_bytes: bytes) -> None:
        """Append one already-WAV-framed clip to the call-recording buffer
        (bounded so a very long call can't exhaust memory). Shared by the bot
        side (:meth:`on_outbound`, already WAV) and the customer side
        (:meth:`on_utterance`, raw PCM16 wrapped first) so the recording
        captures the full conversation, not just the bot's half."""
        if wav_bytes and len(self._audio_buffer) < _MAX_RECORDING_FRAMES:
            self._audio_buffer.append(wav_bytes)

    async def _end_call_leg(self) -> None:
        """The orchestrator just ended the call (``end_call``'s one-and-only
        ``call_ended`` send) — actually terminate the PSTN leg. A no-op REST
        call when Twilio already knows the call ended (a clean `stop` frame);
        only a genuinely orchestrator-initiated mid-call end (opt-out, a
        scripted close, no-input, budget) needs the REST hangup."""
        if self._bridge is None:
            return
        already_stopped = self._bridge.stopped
        await self._bridge.hangup()
        if already_stopped or not self._state.twilio_call_sid:
            return
        await end_twilio_call(
            call_sid=self._state.twilio_call_sid,
            account_sid=self._settings.twilio_account_sid,
            auth_token=self._settings.twilio_auth_token,
        )

    # -- STT seam -----------------------------------------------------------

    async def transcribe(self, pcm16: bytes, sample_rate: int) -> TranscriptResult | None:
        return await self._clients.transcribe(
            TranscribeRequest(
                session_id=self._state.session_id,
                audio_b64=_pcm16_to_wav_b64(pcm16, sample_rate),
                sample_rate=sample_rate,
                is_final=True,
                audio_format="wav",
                language_hint=self._state.language,
                stt_connection_id=self._state.stt_connection_id,
            )
        )

    # -- barge-in (Gate 1+2) --------------------------------------------------

    async def on_voice_activity(self) -> None:
        """Gate 1 for the buffered ingest (RMS voice detected, not an STT
        partial). Fire-and-forget: a sustained-speech check runs in the
        background so this never blocks the caller (``TelephonyAudioIngest.
        feed()``, which is on the live audio-ingestion path)."""
        if self._bridge is None or not self._bridge.is_bot_speaking:
            return
        task = asyncio.create_task(self._confirm_sustained_voice_activity())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _confirm_sustained_voice_activity(self) -> None:
        """There is no STT partial text on this path to run the full Gate 2
        classifier against (it always reads an empty transcript as a
        backchannel — see BargeInClassifier.is_backchannel). Approximate
        instead with the classifier's own "held long enough to be deliberate
        even without ≥2 words" duration (single_word_interrupt_ms): if the
        caller is STILL talking over the bot after that long, treat it as a
        real interrupt."""
        await asyncio.sleep(self._settings.single_word_interrupt_ms / 1000)
        if self._bridge is None or not self._bridge.is_bot_speaking:
            return  # the turn already resolved normally, or the bot stopped
        await self._confirm_barge_in(reason="sustained_speech")

    async def on_partial(self, text: str) -> None:
        """Gate 1+2 for the streaming ingest — an interim STT partial doubles
        as this transport's voice-activity signal (see bargein_gate.py)."""
        if self._bridge is None or not text.strip():
            return
        await self._bridge.send(
            ServerMessage.of(ServerMessageType.partial_transcript, text=text, speaker="customer")
        )
        if not self._bridge.is_bot_speaking:
            self._partial_gate.reset()
            return
        extra = {w.lower() for w in self._state.backchannels.get(self._state.language.value, [])}
        decision = self._partial_gate.evaluate_partial(
            text=text, language=self._state.language, extra_backchannels=extra or None
        )
        if not decision.interrupt:
            return
        await self._confirm_barge_in(reason=decision.reason)

    async def _confirm_barge_in(self, *, reason: str) -> None:
        if self._bridge is None:
            return
        if self._state.transcript and self._state.transcript[-1].speaker == "bot":
            self._state.transcript[-1].interrupted = True
        await self._bridge.send_clear()
        logger.info(
            "telephony_barge_in",
            extra={"extra_fields": {"session_id": self._state.session_id, "reason": reason}},
        )

    # -- utterance handling (smart-turn hold + speculative RAG + escalation) --

    async def on_utterance(self, stt: TranscriptResult, pcm16: bytes) -> None:
        self._partial_gate.reset()
        if self._bridge is None:
            return
        # Capture what the customer actually said regardless of call state —
        # a full call recording (both sides) must include hold-period speech
        # too, not just the turns the bot goes on to answer.
        if pcm16:
            self._buffer_recording_audio(pcm16_to_wav(pcm16, _TELEPHONY_AUDIO_SAMPLE_RATE))
        # On agent hold (3.1): the human operator owns the conversation; drop
        # caller audio rather than run a bot turn over the handoff (mirrors
        # ws/handler.CallSession._handle_audio's waiting_agent guard). Already
        # ended: nothing left to answer (mirrors the same guard there too).
        if self._state.status in (CallStatus.waiting_agent, CallStatus.ended):
            return
        self._bridge.mark_customer_turn()
        self._last_input_at = time.monotonic()
        self._turn_in_at = time.monotonic()

        if self._pending_turn is not None:
            # Follow-up landed inside the merge window: combine with the held
            # mid-thought fragment and answer the whole thought at once.
            held, timer = self._pending_turn
            self._pending_turn = None
            timer.cancel()
            stt = stt.model_copy(update={"text": merge_transcripts(held.text, stt.text)})
        elif self._smart_turn is not None and pcm16 and stt.text.strip():
            # Semantic end-of-turn gate: if the audio says the speaker paused
            # mid-thought, hold the transcript briefly instead of answering
            # half a sentence.
            pcm16_b64 = base64.b64encode(pcm16).decode()
            if await self._smart_turn.is_complete(pcm16_b64) is False:
                timer = asyncio.create_task(self._flush_pending_turn())
                self._pending_turn = (stt, timer)
                self._spec.start(stt.text)
                return

        prefetched = await self._spec.resolve(stt.text)
        await self._orch.handle_final_transcript(
            self._state, self._bridge.send, stt, prefetched_chunks=prefetched
        )
        await self._sync_escalation()
        await self._store.save(self._state)

    async def _flush_pending_turn(self) -> None:
        """Merge window elapsed with no follow-up — the pause was just a
        pause. Answer the held transcript as-is."""
        await asyncio.sleep(self._settings.smart_turn_hold_ms / 1000)
        if self._pending_turn is None or self._bridge is None:
            return
        held, _ = self._pending_turn
        self._pending_turn = None
        prefetched = await self._spec.resolve(held.text)
        await self._orch.handle_final_transcript(
            self._state, self._bridge.send, held, prefetched_chunks=prefetched
        )
        await self._sync_escalation()
        await self._store.save(self._state)

    # -- human handoff (3.1) --------------------------------------------------

    async def _sync_escalation(self) -> None:
        """On the first turn that parks the call in waiting_agent: persist the
        handoff to the queue and register this call's live operator bridge so
        an operator's replies reach the caller and a resolve ends the call —
        mirrors ws/handler.CallSession._sync_escalation. Previously nothing
        called this for telephony at all, so a PSTN call that escalated was
        never actually reachable by an operator."""
        if self._state.status != CallStatus.waiting_agent or self._escalation_id is not None:
            return
        self._escalation_id = await create_escalation(
            self._state, reason=self._state.escalation_reason or "escalation"
        )

        async def _deliver(text: str) -> None:
            if self._bridge is not None:
                await self._orch.deliver_agent_turn(self._state, self._bridge.send, text=text)

        async def _resolve() -> None:
            if self._bridge is not None:
                await self._orch.end_call(self._state, self._bridge.send, outcome="escalated")

        self._escalations.register(self._state.session_id, deliver=_deliver, resolve=_resolve)
        logger.info(
            "escalation_queued",
            extra={"extra_fields": {
                "session_id": self._state.session_id, "escalation_id": self._escalation_id,
                "reason": self._state.escalation_reason,
            }},
        )

    # -- no-input watchdog ----------------------------------------------------

    async def _no_input_watchdog(self) -> None:
        """Background task racing bridge.run(): re-prompts a silent caller,
        then ends the call — mirrors ws/handler.CallSession's no-input timer.
        Telephony has no playback_done signal from the carrier, so the
        playback-finish estimate (``_speech_until``, built the same way the
        browser's is from each chunk's declared duration_ms) stands in for it."""
        try:
            while True:
                await asyncio.sleep(_NO_INPUT_POLL_INTERVAL_S)
                if self._bridge is None or self._state.status in (
                    CallStatus.ended, CallStatus.waiting_agent, CallStatus.escalating,
                ):
                    continue
                window_ms = (
                    self._settings.telephony_no_input_reprompt_ms
                    if self._state.no_input_strikes == 0
                    else self._settings.telephony_no_input_close_ms
                )
                baseline = max(self._last_input_at, self._speech_until)
                remaining = (baseline + window_ms / 1000) - time.monotonic()
                if remaining <= 0:
                    await self._handle_no_input()
        except asyncio.CancelledError:
            raise

    async def _handle_no_input(self) -> None:
        if self._bridge is None or self._state.status in (
            CallStatus.ended, CallStatus.escalating, CallStatus.waiting_agent,
        ):
            return
        outcome = on_no_input(self._state.no_input_strikes, self._state.language)
        logger.info(
            outcome.event,
            extra={"extra_fields": {
                "session_id": self._state.session_id, "event": outcome.event,
                "strikes": self._state.no_input_strikes,
            }},
        )
        if outcome.action == NoInputAction.reprompt:
            self._state.no_input_strikes += 1
            # Restart the window from now — telephony has no precise
            # playback-finish signal for the re-prompt itself either.
            self._last_input_at = time.monotonic()
            await self._orch._send_bot_turn(  # noqa: SLF001 — internal helper reuse
                self._state, self._bridge.send, text=outcome.text, tone=self._state.default_tone
            )
        else:
            self._state.outcome = "customer_unresponsive"
            await self._orch._send_bot_turn(  # noqa: SLF001
                self._state, self._bridge.send, text=outcome.text, tone=self._state.default_tone
            )
            if self._state.customer_id:
                await self._clients.flag_customer(self._state.customer_id, "retry", "no input")
            await self._orch.end_call(self._state, self._bridge.send, outcome="customer_unresponsive")
        await self._store.save(self._state)
