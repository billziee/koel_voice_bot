"""VoiceBot_svc — the orchestrator app. Run: uvicorn koel.voicebot.main:app --port 8100

Wires the REST API + the authenticated WebSocket call endpoint, and warms the
filler-audio cache at startup so latency masking is ready on the first slow turn.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from koel.common.app_factory import create_app
from koel.common.auth import authenticate_websocket
from koel.common.logging import get_logger
from koel.common.schemas import Language
from koel.tts.schemas import SynthesizeRequest
from koel.voicebot.api import analytics, auth, bots, history, knowledge, sessions
from koel.voicebot.config import get_voicebot_settings
from koel.voicebot.deps import build_runtime
from koel.voicebot.pipeline.filler import FILLER_PHRASES, filler_tone_for
from koel.voicebot.session.models import CallStatus
from koel.voicebot.ws.handler import handle_connection

logger = get_logger("koel.voicebot")
settings = get_voicebot_settings()


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


# How long the lifespan waits for in-flight WS calls to finish on SIGTERM (P).
_DRAIN_TIMEOUT_S = 12.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = build_runtime(settings)
    app.state.runtime = runtime
    app.state.active_calls = 0
    logger.info("voicebot_svc starting; redis_ok=%s", await runtime.store.ping())
    await _warm_filler_cache(runtime)
    yield
    # Graceful SIGTERM drain (P): stop accepting new calls, let live ones finish
    # (or hit the grace-TTL finalizer) before tearing down pools.
    waited = 0.0
    while app.state.active_calls > 0 and waited < _DRAIN_TIMEOUT_S:
        logger.info("draining_ws_calls active=%d", app.state.active_calls)
        await asyncio.sleep(0.5)
        waited += 0.5
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
            clients=runtime.clients,
            resumed=resumed,
            background_tasks=runtime.background_tasks,
        )
    finally:
        websocket.app.state.active_calls -= 1


# Re-export the supported-language set for clients/tests.
SUPPORTED_LANGUAGES = {Language.en, Language.hi, Language.hinglish}
