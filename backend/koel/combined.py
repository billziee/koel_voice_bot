"""Single-process deployment of all KOEL services (memory-constrained hosts).

Dev runs five separate uvicorn containers; a 512 MB free-tier box can't hold five
Python processes. This app mounts the existing service apps UNCHANGED into one
process (one set of imports ≈ 300 MB) and runs each one's lifespan via
``lifespan_context`` (Starlette does not auto-run mounted sub-app lifespans).

Routing: voicebot is the public app at ``/`` (REST + the /ws/calls socket); the
AI/CRM services sit under path prefixes. Point the inter-service URLs at this same
process, e.g. ``KOEL_STT_SVC_URL=http://localhost:$PORT/stt`` — the fan-out loops
back in-process (sub-ms), so co-located latency is preserved.

Run: uvicorn koel.combined:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from koel.crm.main import app as crm_app
from koel.nlp.main import app as nlp_app
from koel.stt.main import app as stt_app
from koel.tts.main import app as tts_app
from koel.voicebot.main import app as voicebot_app

_SUBAPPS = (voicebot_app, stt_app, nlp_app, tts_app, crm_app)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Run every mounted sub-app's own startup/shutdown (build runtimes, embedder,
    # providers, filler warmup) — Starlette won't do this for mounts automatically.
    async with AsyncExitStack() as stack:
        for sub in _SUBAPPS:
            await stack.enter_async_context(sub.router.lifespan_context(sub))
        yield


app = FastAPI(title="KOEL · combined", lifespan=_lifespan)

# Specific prefixes first; voicebot as the "/" catch-all last (order matters).
app.mount("/stt", stt_app)
app.mount("/nlp", nlp_app)
app.mount("/tts", tts_app)
app.mount("/crm", crm_app)
app.mount("/", voicebot_app)
