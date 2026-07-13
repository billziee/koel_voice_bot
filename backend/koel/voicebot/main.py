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

from fastapi import FastAPI, WebSocket

from koel.common.app_factory import create_app
from koel.common.auth import authenticate_websocket
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
)
from koel.voicebot.config import get_voicebot_settings
from koel.voicebot.deps import build_runtime
from koel.voicebot.inbound import build_inbound_session
from koel.voicebot.pipeline import localization
from koel.voicebot.pipeline.filler import FILLER_PHRASES, filler_tone_for
from koel.voicebot.pipeline.transliteration import SarvamTransliterator
from koel.voicebot.session.models import CallStatus, SessionState
from koel.voicebot.telephony.bridge import TwilioMediaBridge
from koel.voicebot.telephony.ingest import (
    PartialCallback,
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
    yield
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

    # Concurrency guard: only ONE live socket per session at a time. A second
    # concurrent connect (double-tab, racey reconnect) would resume the same
    # state and clobber turns on write-back — reject it. (Single-worker scope;
    # multi-worker needs a Redis lock — see docs/SECURITY.md.)
    active_sessions = websocket.app.state.active_sessions
    if session_id in active_sessions:
        await websocket.close(code=1008, reason="session already active")
        return
    active_sessions.add(session_id)

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
    signature = websocket.headers.get("x-twilio-signature", "")
    signed_url = f"{settings.public_base_url}/twilio/stream"
    params = dict(websocket.query_params)
    if not verify_twilio_signature(signed_url, params, signature, settings.twilio_auth_token):
        await websocket.close(code=1008, reason="invalid twilio signature")
        return

    # (2) Inbound needs a bound bot: the WS URL carries no session_id (unlike the
    # browser path), so a configured default BotConfig identifies the callee.
    if not settings.twilio_default_bot_id:
        await websocket.close(code=1008, reason="inbound telephony disabled")
        return

    runtime = websocket.app.state.runtime
    async with session_scope() as db:
        bot: BotConfig | None = await db.get(BotConfig, settings.twilio_default_bot_id)
    if bot is None or not bot.active:
        await websocket.close(code=1008, reason="inbound bot unavailable")
        return

    await websocket.accept()

    # (4) Seed the call. The caller's number may arrive as a templated WS query
    # param (e.g. <Stream url=".../twilio/stream?From={{From}}">). Inbound (3.2):
    # resolve the caller by ANI → CRM lookup → intent-first session; an unknown
    # or absent number still yields an anonymous inbound session.
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


# Re-export the supported-language set for clients/tests.
SUPPORTED_LANGUAGES = {Language.en, Language.hi, Language.hinglish}
