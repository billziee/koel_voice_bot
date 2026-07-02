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

import asyncio
import logging
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI

from koel.common.config import get_common_settings
from koel.crm.main import app as crm_app
from koel.nlp.main import app as nlp_app
from koel.stt.main import app as stt_app
from koel.tts.main import app as tts_app
from koel.voicebot.main import app as voicebot_app

logger = logging.getLogger("koel.combined")

_SUBAPPS = (voicebot_app, stt_app, nlp_app, tts_app, crm_app)

# A single ping shouldn't outlive a cold-start wake (~50s); cap it so a slow tick
# can't stack onto the next interval.
_KEEPALIVE_TIMEOUT_S = 45.0


async def _keepalive_loop(url: str, interval_s: int) -> None:
    """Ping our own public ``/health`` on an interval so an idle-spin-down host
    (e.g. Render free tier) stays warm between demos.

    The request leaves the box and re-enters through the host's router, which is
    what resets its idle timer — an in-process no-op would not. Never dies on a
    failed ping (transient network / brief downtime); cancellation exits cleanly.
    """
    from koel.common.http import make_async_client

    ping = url.rstrip("/") + "/health"
    async with make_async_client(timeout_s=_KEEPALIVE_TIMEOUT_S) as client:
        while True:
            await asyncio.sleep(interval_s)  # sleep first: we boot warm
            try:
                resp = await client.get(ping)
                logger.debug("keepalive %s → %s", ping, resp.status_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed ping must not kill the loop
                logger.warning("keepalive ping failed: %s", exc)


async def _ensure_schema() -> None:
    """Apply migrations + seed on boot when the schema is missing/empty.

    Render free tier runs no pre-deploy/one-off jobs, so the app self-migrates.
    Idempotent: skips seeding when bots already exist (no KB re-embed per boot).
    """
    from sqlalchemy import func, select

    from koel.db.models import BotConfig
    from koel.db.session import session_scope

    need = True
    try:
        async with session_scope() as db:
            count = (await db.execute(select(func.count()).select_from(BotConfig))).scalar_one()
            need = count == 0
    except Exception:  # noqa: BLE001 — tables don't exist yet → bootstrap
        need = True
    if not need:
        return
    logger.info("schema missing/empty → running migrations + seed")
    # `python -m` puts CWD (the WORKDIR that holds scripts/) on sys.path; the
    # bootstrap script runs alembic upgrade head then seeds. Run off the loop.
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "scripts.bootstrap_db"], check=True
    )


@asynccontextmanager
async def _lifespan(_: FastAPI):
    await _ensure_schema()
    settings = get_common_settings()
    keepalive: asyncio.Task[None] | None = None
    # Run every mounted sub-app's own startup/shutdown (build runtimes, embedder,
    # providers, filler warmup) — Starlette won't do this for mounts automatically.
    async with AsyncExitStack() as stack:
        for sub in _SUBAPPS:
            await stack.enter_async_context(sub.router.lifespan_context(sub))
        if settings.keepalive_url:
            keepalive = asyncio.create_task(
                _keepalive_loop(settings.keepalive_url, settings.keepalive_interval_seconds)
            )
            logger.info(
                "keepalive: self-ping %s/health every %ss",
                settings.keepalive_url.rstrip("/"),
                settings.keepalive_interval_seconds,
            )
        try:
            yield
        finally:
            # Stop pinging before the sub-app resources it depends on are torn down.
            if keepalive is not None:
                keepalive.cancel()
                with suppress(asyncio.CancelledError):
                    await keepalive


app = FastAPI(title="KOEL · combined", lifespan=_lifespan)

# Specific prefixes first; voicebot as the "/" catch-all last (order matters).
app.mount("/stt", stt_app)
app.mount("/nlp", nlp_app)
app.mount("/tts", tts_app)
app.mount("/crm", crm_app)
app.mount("/", voicebot_app)
