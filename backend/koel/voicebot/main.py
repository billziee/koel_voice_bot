"""VoiceBot_svc — the orchestrator app. Run: uvicorn koel.voicebot.main:app --port 8100

Wires the REST API + the authenticated WebSocket call endpoint, and warms the
filler-audio cache at startup so latency masking is ready on the first slow turn.
"""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, status

from koel.common.app_factory import create_app
from koel.common.auth import authenticate_websocket
from koel.common.errors import ApiError
from koel.common.logging import get_logger, set_correlation_id
from koel.common.model_connections import resolve_connection
from koel.common.schemas import Language
from koel.db.models import BotConfig
from koel.db.session import session_scope
from koel.stt.config import get_stt_settings
from koel.stt.schemas import TranscribeRequest, TranscriptResult
from koel.stt.streaming_session import DeepgramStreamingSession
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.api import (
    analytics,
    auth,
    bots,
    connections,
    exports,
    history,
    knowledge,
    operators,
    orgs,
    sessions,
    users,
)
from koel.voicebot.api.sessions import _minimise_attributes, build_session_state
from koel.voicebot.campaigns import get_attempt, record_dial_result
from koel.voicebot.config import get_voicebot_settings
from koel.voicebot.deps import build_runtime
from koel.voicebot.inbound import build_inbound_session
from koel.voicebot.pipeline import localization
from koel.voicebot.pipeline.dialer_worker import dialer_enabled, run_dialer_loop
from koel.voicebot.pipeline.filler import FILLER_PHRASES, filler_tone_for
from koel.voicebot.pipeline.outbound_dialer import (
    call_outcome_to_dial_outcome,
    call_status_to_dial_outcome,
)
from koel.voicebot.pipeline.transliteration import SarvamTransliterator
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.telephony.bridge import TwilioMediaBridge
from koel.voicebot.telephony.ingest import (
    PartialCallback,
    StreamingSessionFactory,
    StreamingTelephonyIngest,
    TelephonyAudioIngest,
    TranscribeCallback,
    UtteranceCallback,
)
from koel.voicebot.telephony.twilio import verify_twilio_signature
from koel.voicebot.ws.handler import handle_connection
from koel.voicebot.ws.protocol import ServerMessage, ServerMessageType

logger = get_logger("koel.voicebot")
settings = get_voicebot_settings()
# STT settings drive the telephony streaming-STT decision (Deepgram env default +
# BYOK connection resolution). Module-level so tests can monkeypatch it, mirroring
# ``settings``. lru_cached, so this is the same instance the STT service sees.
stt_settings = get_stt_settings()

# Twilio media decodes to 16 kHz PCM16 (the bridge's default ``inbound_sample_rate``);
# the streaming Deepgram session must be opened at that same rate.
_TELEPHONY_STT_SAMPLE_RATE = 16000

# The browser's neural-VAD worklet emits raw s16le mono 16 kHz PCM (``pcm16_b64`` on
# each audio_chunk — see ws/protocol.py), so the browser streaming Deepgram session
# opens at the same rate.
_BROWSER_STT_SAMPLE_RATE = 16000


# Per-phrase warmup timeout: a slow provider must not block boot. 9 sequential
# calls × a 30s cold-start could stall startup ~270s — so we bound + parallelise.
_WARMUP_PHRASE_TIMEOUT_S = 5.0


async def _warm_filler_cache(runtime) -> None:
    """Pre-render filler clips per language via TTS_svc (PRD §05 Latency Masking).

    Phrases warm concurrently with a per-phrase timeout (E): a slow/cold provider
    skips that clip instead of serialising the whole boot behind it.
    """

    async def _one(phrase, language):  # noqa: ANN001
        try:
            result = await asyncio.wait_for(
                runtime.clients.synthesize(
                    SynthesizeRequest(
                        session_id="filler-warmup",
                        text=phrase,
                        language=language,
                        tone=filler_tone_for("credit_card"),
                    )
                ),
                timeout=_WARMUP_PHRASE_TIMEOUT_S,
            )
            return result.audio_b64
        except (Exception, TimeoutError):  # noqa: BLE001 — warm lazily later on failure
            return None

    for language, phrases in FILLER_PHRASES.items():
        results = await asyncio.gather(*(_one(p, language) for p in phrases))
        clips = [c for c in results if c]
        if clips:
            runtime.filler.load(language, clips)
        else:
            logger.warning("filler_warm_failed language=%s", language.value)


async def _warm_localization() -> None:
    """Derive Hinglish operational copy from its Devanagari source (PRD §06
    dual-script collapse). Runs before the filler-cache warm so the filler
    phrases it TTS-synthesizes below already reflect the live transliteration.
    Best-effort — localization.warm() itself never raises."""
    if not settings.sarvam_enabled:
        return
    transliterator = SarvamTransliterator(
        api_key=settings.sarvam_api_key,  # type: ignore[arg-type]  # sarvam_enabled guards None
        base_url=settings.sarvam_base_url,
        timeout_s=settings.sarvam_translit_timeout_s,
    )
    try:
        await localization.warm(transliterator.to_hinglish)
    finally:
        await transliterator.aclose()


# How long the lifespan waits for in-flight WS calls to finish on SIGTERM (P).
_DRAIN_TIMEOUT_S = 12.0


def warn_if_turnstile_unenforced(cfg) -> bool:  # noqa: ANN001 — CommonSettings subclass
    """Warn (once, at startup) when production runs with no Turnstile secret (6.7).

    The login challenge is enforced only when KOEL_TURNSTILE_SECRET_KEY is set,
    but the frontend widget is gated on a SEPARATE build-time key — so a prod
    deploy can present a solved challenge while the backend verifies nothing.
    This surfaces that split loudly rather than silently. NOT a hard fail:
    Turnstile is optional by design (unlike the jwt_secret/CORS prod guards).
    Returns whether the warning fired (for tests).
    """
    if cfg.is_production and not cfg.turnstile_secret_key:
        logger.warning(
            "turnstile_disabled_in_production: KOEL_TURNSTILE_SECRET_KEY is unset — "
            "/v1/auth/login accepts any request without a bot-challenge. If the "
            "frontend was built with VITE_TURNSTILE_SITE_KEY, users see a challenge "
            "the backend does NOT verify."
        )
        return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = build_runtime(settings)
    app.state.runtime = runtime
    app.state.active_calls = 0
    app.state.active_sessions = set()  # session_ids with a live socket (concurrency guard)
    logger.info("voicebot_svc starting; redis_ok=%s", await runtime.store.ping())
    warn_if_turnstile_unenforced(settings)
    await _warm_localization()
    await _warm_filler_cache(runtime)
    # Outbound dialer (1.2): dormant unless the full Twilio origination config
    # is set (mirrors every other carrier-credential gate in this codebase).
    # An infinite loop, so it's CANCELLED on shutdown below — not awaited
    # alongside the grace finalizers, which are expected to run to completion.
    dialer_task: asyncio.Task | None = None
    if dialer_enabled(settings):
        dialer_task = asyncio.create_task(
            run_dialer_loop(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                from_number=settings.twilio_from_number,
                public_base_url=settings.public_base_url,
                default_bot_id=settings.twilio_default_bot_id,
                crm=runtime.clients,
                calling_hours=settings,
                poll_interval_s=settings.dialer_poll_interval_s,
                batch_size=settings.dialer_batch_size,
            )
        )
        logger.info("outbound_dialer_started")
    # Cross-process escalation fan-in (3.1): dormant with no redis_client, but
    # build_runtime always wires one (Redis is required infra here, unlike the
    # carrier/AWS integrations above) — so this always runs. Infinite loop,
    # cancelled on shutdown like the dialer.
    escalation_listener_task = asyncio.create_task(runtime.escalations.run_listener())
    yield
    if dialer_task is not None:
        dialer_task.cancel()
        await asyncio.gather(dialer_task, return_exceptions=True)
    escalation_listener_task.cancel()
    await asyncio.gather(escalation_listener_task, return_exceptions=True)
    # Graceful SIGTERM drain (P): stop accepting new calls, let live ones finish
    # (or hit the grace-TTL finalizer) before tearing down pools.
    waited = 0.0
    while app.state.active_calls > 0 and waited < _DRAIN_TIMEOUT_S:
        logger.info("draining_ws_calls active=%d", app.state.active_calls)
        await asyncio.sleep(0.5)
        waited += 0.5
    # Let in-flight grace finalizers (sleeping on the 15s reconnect window) run to
    # completion so a call dropped just before shutdown is still persisted — else
    # their sleep is cancelled mid-flight and the call is lost.
    pending = [t for t in runtime.background_tasks if not t.done()]
    if pending:
        logger.info("awaiting_%d_grace_finalizers_before_shutdown", len(pending))
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=settings.session_grace_ttl_seconds + 5,
            )
        except TimeoutError:
            logger.warning("grace_finalizers_did_not_drain_in_time")
    await runtime.clients.aclose()
    await runtime.store.close()
    if runtime.shared_redis is not None:
        await runtime.shared_redis.aclose()
    await knowledge.aclose_embedder()


async def _readiness() -> dict[str, bool]:
    """Readiness probe (P): DB + Redis reachable. Best-effort per dependency."""
    from sqlalchemy import text

    from koel.db.session import get_engine

    checks: dict[str, bool] = {}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        checks["postgres"] = False
    try:
        checks["redis"] = bool(await app.state.runtime.store.ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False
    return checks


app = create_app(
    service_name="voicebot_svc",
    title="KOEL · VoiceBot_svc",
    description="Orchestrator: session lifecycle, STT→NLP→TTS routing, barge-in, filler, recovery.",
    settings=settings,
    lifespan=lifespan,
    readiness_check=_readiness,
)

app.include_router(auth.router)
app.include_router(bots.router)
app.include_router(sessions.router)
app.include_router(history.router)
app.include_router(analytics.router)
app.include_router(knowledge.router)
app.include_router(operators.router)
app.include_router(orgs.router)
app.include_router(users.router)
app.include_router(connections.router)
app.include_router(exports.router)


@app.websocket("/ws/calls/{session_id}")
async def call_socket(websocket: WebSocket, session_id: str) -> None:
    """Authenticated call socket (PRD §08). Resumes within the 15s grace window."""
    claims = await authenticate_websocket(websocket)
    if claims is None:
        return  # socket already closed with 1008

    runtime = websocket.app.state.runtime
    state = await runtime.store.load(session_id)
    if state is None:
        # Must POST /v1/sessions first to seed CRM + bot config.
        await websocket.close(code=1008, reason="unknown session")
        return

    # AuthZ: the socket's token sub must own this session — no attaching to or
    # eavesdropping another operator's live call (PRD §08 access control).
    if state.owner_sub is not None and claims.sub != state.owner_sub:
        await websocket.close(code=1008, reason="not session owner")
        return

    resumed = state.status in (CallStatus.reconnecting, CallStatus.ended) and bool(state.transcript)
    if resumed:
        # EXPIRE returns False if the key already expired between load and now
        # (grace window elapsed) — treat as an unknown/expired session.
        if not await runtime.store.restore_active_ttl(session_id):
            await websocket.close(code=1008, reason="session expired")
            return
        # Claim the session off the grace path NOW, before any further await: flip
        # `reconnecting` → `listening` and persist immediately. The prior session's
        # detached grace finalizer (_await_grace_then_finalize) re-loads state and
        # bails when it's no longer `reconnecting`, so persisting the reclaim here —
        # rather than late in run() after accept()+the reconnected frame — closes
        # the window where a reconnect landing near grace-expiry would still be
        # finalized as a dropped call. (Sub-millisecond Redis GET/SET interleave
        # residual needs a store-level CAS under multi-worker — see docs/SECURITY.md.)
        if state.status == CallStatus.reconnecting:
            state.status = CallStatus.listening
            await runtime.store.save(state)

    # Concurrency guard: only ONE live socket per session at a time. A second
    # concurrent connect (double-tab, racey reconnect) would resume the same
    # state and clobber turns on write-back — reject it. (Single-worker scope;
    # multi-worker needs a Redis lock — see docs/SECURITY.md.)
    active_sessions = websocket.app.state.active_sessions
    if session_id in active_sessions:
        await websocket.close(code=1008, reason="session already active")
        return
    active_sessions.add(session_id)

    # O-6 Wave 2: when this call resolves to Deepgram streaming STT, hand the
    # CallSession a factory that opens a long-lived DeepgramStreamingSession
    # (continuous frames → live partials + finals). None ⇒ the unchanged buffered
    # per-utterance transcribe path (every non-streaming bot). Resolved before the
    # socket runs; the session itself opens lazily on the first audio frame.
    streaming_session_factory = await _browser_streaming_factory(state)

    # Track in-flight calls so the lifespan can drain them on SIGTERM (P).
    websocket.app.state.active_calls += 1
    try:
        await handle_connection(
            websocket,
            session_id=session_id,
            state=state,
            settings=settings,
            store=runtime.store,
            orchestrator=runtime.orchestrator,
            bargein=runtime.bargein,
            smart_turn=runtime.smart_turn,
            clients=runtime.clients,
            resumed=resumed,
            background_tasks=runtime.background_tasks,
            escalations=runtime.escalations,
            monitor=runtime.monitor,
            streaming_session_factory=streaming_session_factory,
        )
    finally:
        websocket.app.state.active_calls -= 1
        active_sessions.discard(session_id)


def _pcm16_to_wav_b64(pcm16: bytes, sample_rate: int) -> str:
    """Wrap raw PCM16 mono samples in a WAV container (base64) for STT_svc.

    Twilio delivers 8 kHz µ-law; the bridge decodes it to raw 16 kHz PCM16. STT
    infers the codec from the container, so the endpointed utterance is framed as
    a WAV — the exact inverse of the bridge's own ``_wav_to_pcm16``.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)
    return base64.b64encode(buffer.getvalue()).decode()


async def _resolve_deepgram_streaming(state: SessionState) -> tuple[str, str, str] | None:
    """Decide whether this call runs on Deepgram *streaming* STT (O-6 Wave 2).

    Returns ``(api_key, model, base_url)`` when it does, else ``None`` (→ buffered
    ingest). A bot's own Deepgram STT :class:`ModelConnection` wins (BYOK); failing
    that, the env default (``stream_stt_provider=deepgram`` + a configured Deepgram
    key) applies. Any resolution error degrades to ``None`` — never raises.
    """
    conn_id = state.stt_connection_id
    if conn_id:
        try:
            resolved = await resolve_connection(conn_id, state.org_id)
        except Exception:  # noqa: BLE001 — a resolve failure just falls through to the env default
            resolved = None
        if (
            resolved is not None
            and resolved.kind == "stt"
            and (resolved.provider or "").lower() == "deepgram"
            and resolved.api_key
        ):
            return (
                resolved.api_key,
                resolved.model or stt_settings.deepgram_model,
                resolved.base_url or stt_settings.deepgram_base_url,
            )
    key = stt_settings.deepgram_api_key
    if (
        stt_settings.stream_stt_provider.lower() == "deepgram"
        and stt_settings.deepgram_enabled
        and key
    ):
        return (key, stt_settings.deepgram_model, stt_settings.deepgram_base_url)
    return None


async def _browser_streaming_factory(state: SessionState) -> StreamingSessionFactory | None:
    """A session factory for the browser (``/ws/calls``) streaming STT path (O-6 Wave 2).

    Returns a factory that opens a long-lived :class:`DeepgramStreamingSession` when
    this call resolves to Deepgram *streaming* STT (reusing the same BYOK-or-env
    resolution as telephony via :func:`_resolve_deepgram_streaming`); ``None`` for every
    other bot — the CallSession then runs the unchanged buffered per-utterance transcribe
    path. The socket is opened lazily by the CallSession on the first audio frame, so a
    connect failure degrades to the buffered path in-flight without dropping the call.
    """
    dg = await _resolve_deepgram_streaming(state)
    if dg is None:
        return None
    api_key, model, base_url = dg

    def _factory(
        on_partial: PartialCallback, on_final: UtteranceCallback
    ) -> DeepgramStreamingSession:
        return DeepgramStreamingSession(
            api_key=api_key,
            model=model,
            base_url=base_url,
            sample_rate=_BROWSER_STT_SAMPLE_RATE,
            on_partial=on_partial,
            on_final=on_final,
            language=state.language or Language.en,
            session_id=state.session_id,
        )

    return _factory


async def _build_telephony_ingest(
    *,
    state: SessionState,
    transcribe: TranscribeCallback,
    on_utterance: UtteranceCallback,
    on_partial: PartialCallback,
) -> TelephonyAudioIngest | StreamingTelephonyIngest:
    """Pick the caller-audio ingest for this call.

    Deepgram-streaming calls get a :class:`StreamingTelephonyIngest` backed by a
    long-lived :class:`DeepgramStreamingSession` (continuous frames → live interim
    partials + finals). Everything else — and any failure to open the streaming
    socket — gets the buffered, RMS-endpointed :class:`TelephonyAudioIngest` (the
    unchanged fallback + the path for every non-Deepgram provider). The streaming
    socket is opened here, BEFORE any audio flows, so a connect failure falls back
    cleanly instead of dropping a live turn.
    """
    dg = await _resolve_deepgram_streaming(state)
    if dg is not None:
        api_key, model, base_url = dg

        def _factory(
            partial: PartialCallback, final: UtteranceCallback
        ) -> DeepgramStreamingSession:
            return DeepgramStreamingSession(
                api_key=api_key,
                model=model,
                base_url=base_url,
                sample_rate=_TELEPHONY_STT_SAMPLE_RATE,
                on_partial=partial,
                on_final=final,
                language=state.language or Language.en,
                session_id=state.session_id,
            )

        streaming = StreamingTelephonyIngest(
            session_factory=_factory, on_utterance=on_utterance, on_partial=on_partial
        )
        if await streaming.start():
            logger.info(
                "telephony_streaming_stt_engaged",
                extra={"extra_fields": {"session_id": state.session_id, "model": model}},
            )
            return streaming
        # Socket didn't open → release it and fall back to the buffered ingest.
        await streaming.aclose()
        logger.warning(
            "telephony_streaming_stt_unavailable; using buffered ingest",
            extra={"extra_fields": {"session_id": state.session_id}},
        )
    return TelephonyAudioIngest(transcribe=transcribe, on_utterance=on_utterance)


async def _build_outbound_telephony_session(runtime, bot: BotConfig, attempt_id: str) -> SessionState:
    """Seed the SessionState for an OUTBOUND campaign-dial leg (1.2) — the
    Stream connected, meaning the customer answered.

    No direction override in crm_context: KOEL's default (the absence of the
    inbound.DIRECTION_KEY override) is the pitch-first outbound prompt,
    matching ``POST /v1/sessions`` (the existing HTTP-triggered outbound
    path) exactly. Calling-hours + DND were already enforced by the dialer
    worker before this call was ever placed — not re-checked here.
    """
    attempt = await get_attempt(attempt_id)
    customer_id = attempt["customer_id"] if attempt else None
    crm_context: dict[str, object] = {}
    if customer_id:
        customer = await runtime.clients.get_customer(customer_id, org_id=bot.org_id)
        if customer is not None:
            crm_context = {
                "name": customer.name,
                "bank": customer.bank,
                "phone": customer.phone,
                **_minimise_attributes(bot.use_case.value, customer.attributes),
            }
    return build_session_state(bot, customer_id=customer_id, crm_context=crm_context)


@app.websocket("/twilio/stream")
async def twilio_stream(websocket: WebSocket) -> None:
    """Inbound PSTN call bridge over Twilio Media Streams (roadmap 1.1 / 1.3).

    DORMANT by default. The Twilio request signature is verified FIRST and fails
    CLOSED when no ``KOEL_TWILIO_AUTH_TOKEN`` is configured, so an unconfigured
    deployment rejects the upgrade (1008) rather than trusting it. Browser calls
    (``/ws/calls/{session_id}``) are entirely unaffected.

    On a configured deployment: verify signature → confirm a default bot is bound
    → accept → seed a :class:`SessionState` (shared with the HTTP path) → wire the
    caller-audio seam through server-side endpointing (:class:`TelephonyAudioIngest`)
    into ``orchestrator.handle_final_transcript`` → run the media bridge.
    """
    # (1) Fail-closed signature check. verify_twilio_signature returns False on an
    # empty auth token, so this is the dormancy gate — no token, no inbound calls.
    # Twilio signs the exact request URL it hit (query string included) with no
    # separately-appended params for a bodyless GET/WS-upgrade request — so the
    # query string must stay IN the URL, not be re-sorted and appended as if it
    # were POST form data (that produces a different, never-matching digest).
    signature = websocket.headers.get("x-twilio-signature", "")
    raw_query = websocket.scope.get("query_string", b"").decode()
    signed_url = f"{settings.public_base_url}/twilio/stream"
    if raw_query:
        signed_url = f"{signed_url}?{raw_query}"
    if not verify_twilio_signature(signed_url, {}, signature, settings.twilio_auth_token):
        await websocket.close(code=1008, reason="invalid twilio signature")
        return

    # (2) The WS URL carries no session_id (unlike the browser path), so the
    # callee bot must come from elsewhere: an outbound leg (the campaign
    # dialer, 1.2) carries its own attempt_id + bot_id query params (added by
    # outbound_dialer.originate_call, protected by the signature check above
    # since they're part of the signed URL); inbound falls back to the
    # configured default BotConfig.
    attempt_id = websocket.query_params.get("attempt_id")
    outbound_bot_id = websocket.query_params.get("bot_id") if attempt_id else None
    bot_id = outbound_bot_id or settings.twilio_default_bot_id
    if not bot_id:
        await websocket.close(code=1008, reason="inbound telephony disabled")
        return

    runtime = websocket.app.state.runtime
    async with session_scope() as db:
        bot: BotConfig | None = await db.get(BotConfig, bot_id)
    if bot is None or not bot.active:
        await websocket.close(code=1008, reason="inbound bot unavailable")
        return

    await websocket.accept()

    # (4) Seed the call.
    if attempt_id:
        # Outbound (1.2): the dialer already ran the calling-hours + DND gate
        # before ever placing this call — no direction override (KOEL's
        # default, matching the HTTP-triggered outbound path, is the
        # pitch-first prompt; only inbound needs the intent-first override).
        state = await _build_outbound_telephony_session(runtime, bot, attempt_id)
    else:
        # Inbound (3.2): the caller's number may arrive as a templated WS
        # query param (e.g. <Stream url=".../twilio/stream?From={{From}}">).
        # Resolve the caller by ANI → CRM lookup → intent-first session; an
        # unknown or absent number still yields an anonymous inbound session.
        caller_phone = websocket.query_params.get("From") or websocket.query_params.get("from")
        state = await build_inbound_session(runtime.clients, bot, caller_phone=caller_phone)
    set_correlation_id(state.session_id)
    await runtime.store.save(state)

    # (5) STT seam: wrap clients.transcribe (mirrors ws/handler._handle_audio —
    # final utterance, language pinned to the bot) behind the endpointer.
    async def _transcribe(pcm16: bytes, sample_rate: int) -> TranscriptResult | None:
        return await runtime.clients.transcribe(
            TranscribeRequest(
                session_id=state.session_id,
                audio_b64=_pcm16_to_wav_b64(pcm16, sample_rate),
                sample_rate=sample_rate,
                is_final=True,
                audio_format="wav",
                language_hint=state.language,
                stt_connection_id=state.stt_connection_id,  # BYOK per-bot STT connection
            )
        )

    async def _on_utterance(stt: TranscriptResult) -> None:
        # bridge.send translates the orchestrator's reply back into Twilio frames.
        if bridge is None:
            return
        await runtime.orchestrator.handle_final_transcript(state, bridge.send, stt)

    async def _send_partial(text: str) -> None:
        """Forward an interim STT partial through the transport as a best-effort
        ``partial_transcript`` (O-6). The Twilio bridge has no carrier
        representation for it and drops it; on a browser transport it surfaces as a
        live caption. NEVER authoritative — the turn still runs on the final."""
        if bridge is None or not text.strip():
            return
        await bridge.send(
            ServerMessage.of(ServerMessageType.partial_transcript, text=text, speaker="customer")
        )

    # (6) STT ingest: the long-lived Deepgram streaming session (true continuous
    # partials) when this call resolves to Deepgram streaming STT, else the
    # buffered, RMS-endpointed TelephonyAudioIngest (fallback + non-Deepgram path).
    # Bound to a mutable ``bridge`` the closures above read at call time (feeds only
    # start once bridge.run() is underway, so it is always assigned by then).
    bridge: TwilioMediaBridge | None = None
    ingest = await _build_telephony_ingest(
        state=state,
        transcribe=_transcribe,
        on_utterance=_on_utterance,
        on_partial=_send_partial,
    )

    # (7) Run the media bridge. Constructed directly (this is exactly what
    # run_twilio_bridge does) so on_utterance can reach the bridge's own send.
    dtmf_map = bot.config.get("dtmf_map", {}) if isinstance(bot.config, dict) else {}
    bridge = TwilioMediaBridge(
        websocket=websocket,
        orchestrator=runtime.orchestrator,
        state=state,
        dtmf_map=dtmf_map,
        on_caller_audio=ingest.feed,
    )

    websocket.app.state.active_calls += 1
    try:
        await bridge.run()
    finally:
        # Drain any speech still buffered when the stream stopped — unless the
        # call already ended normally (a `stop`/hangup already ran end_call), in
        # which case there's no one left to answer.
        if state.status != CallStatus.ended:
            await ingest.flush()
        # Always tear the ingest down: the streaming ingest owns a Deepgram socket
        # that must close even on a normal end; the buffered ingest has no aclose.
        aclose = getattr(ingest, "aclose", None)
        if aclose is not None:
            await aclose()
        websocket.app.state.active_calls -= 1
        # Outbound (1.2): report this leg's outcome back to the dial queue so
        # the retry policy can act on it — a customer never reached (dropped
        # mid-ring, etc.) should be retried; one who was must not be.
        if attempt_id:
            await record_dial_result(attempt_id, call_outcome_to_dial_outcome(state.outcome))


@app.post("/twilio/call-status")
async def twilio_call_status(request: Request) -> dict[str, bool]:
    """Twilio call-status webhook (1.2) — reports outbound legs that never
    reached ``/twilio/stream`` at all (no answer / busy / failed / canceled).
    A leg that DID connect reports its outcome from ``twilio_stream``'s own
    teardown instead; those statuses are ignored here (see
    ``call_status_to_dial_outcome``).

    A real POST webhook with a form body — unlike ``/twilio/stream`` (a
    bodyless WS upgrade), Twilio's signature here covers the URL plus the
    sorted POST params, exactly as ``verify_twilio_signature`` implements.
    """
    form = await request.form()
    attempt_id = request.query_params.get("attempt_id")
    signature = request.headers.get("x-twilio-signature", "")
    url = f"{settings.public_base_url}/twilio/call-status?attempt_id={attempt_id or ''}"
    params = {k: str(v) for k, v in form.items()}
    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="invalid_twilio_signature",
            message="Invalid Twilio signature.",
        )
    if attempt_id:
        outcome = call_status_to_dial_outcome(str(form.get("CallStatus", "")))
        if outcome is not None:
            await record_dial_result(attempt_id, outcome)
    return {"ok": True}


# Re-export the supported-language set for clients/tests.
SUPPORTED_LANGUAGES = {Language.en, Language.hi, Language.hinglish}
