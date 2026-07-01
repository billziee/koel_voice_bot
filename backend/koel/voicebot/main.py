"""VoiceBot_svc — the orchestrator app. Run: uvicorn koel.voicebot.main:app --port 8100

Wires the REST API + the authenticated WebSocket call endpoint, and warms the
filler-audio cache at startup so latency masking is ready on the first slow turn.
"""

from __future__ import annotations

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


async def _warm_filler_cache(runtime) -> None:
    """Pre-render filler clips per language via TTS_svc (PRD §05 Latency Masking)."""
    for language, phrases in FILLER_PHRASES.items():
        clips: list[str] = []
        for phrase in phrases:
            try:
                result = await runtime.clients.synthesize(
                    SynthesizeRequest(
                        session_id="filler-warmup",
                        text=phrase,
                        language=language,
                        tone=filler_tone_for("credit_card"),
                    )
                )
                clips.append(result.audio_b64)
            except Exception:  # noqa: BLE001 — TTS may be down at boot; warm lazily later
                logger.warning("filler_warm_failed language=%s", language.value)
                break
        if clips:
            runtime.filler.load(language, clips)


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = build_runtime(settings)
    app.state.runtime = runtime
    logger.info("voicebot_svc starting; redis_ok=%s", await runtime.store.ping())
    await _warm_filler_cache(runtime)
    yield
    await runtime.clients.aclose()
    await runtime.store.close()
    await knowledge.aclose_embedder()


app = create_app(
    service_name="voicebot_svc",
    title="KOEL · VoiceBot_svc",
    description="Orchestrator: session lifecycle, STT→NLP→TTS routing, barge-in, filler, recovery.",
    settings=settings,
    lifespan=lifespan,
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


# Re-export the supported-language set for clients/tests.
SUPPORTED_LANGUAGES = {Language.en, Language.hi, Language.hinglish}
