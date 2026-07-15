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

# We ping our OWN url while the process is up, so it responds fast — no need to
# wait out a cold-start; a tight bound keeps a stuck request from stacking.
_KEEPALIVE_TIMEOUT_S = 30.0
# After a FAILED ping, retry soon rather than after a full interval, so one blip
# can't open a gap wider than the ~15-min idle cutoff and let the host spin down.
_KEEPALIVE_RETRY_S = 30.0
# Fallback if a caller passes a non-positive interval (would be a busy loop).
_DEFAULT_KEEPALIVE_INTERVAL_S = 300.0


async def _keepalive_loop(url: str, interval_s: float) -> None:
    """Ping our own public ``/health`` on an interval so an idle-spin-down host
    (e.g. Render free tier) stays warm between demos.

    The request leaves the box and re-enters through the host's router, which is
    what resets its idle timer — an in-process no-op would not. Never dies on a
    failed ping (transient network / brief downtime); cancellation exits cleanly.
    """
    from koel.common.http import make_async_client

    ping = url.rstrip("/") + "/health"
    interval_s = interval_s if interval_s > 0 else _DEFAULT_KEEPALIVE_INTERVAL_S
    delay = interval_s
    async with make_async_client(timeout_s=_KEEPALIVE_TIMEOUT_S) as client:
        while True:
            await asyncio.sleep(delay)  # sleep first: we boot warm
            try:
                resp = await client.get(ping)
                logger.debug("keepalive %s → %s", ping, resp.status_code)
                delay = interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed ping must not kill the loop
                logger.warning("keepalive ping failed (retry in %ss): %s", _KEEPALIVE_RETRY_S, exc)
                delay = min(_KEEPALIVE_RETRY_S, interval_s)


async def _ensure_schema() -> None:
    """Migrate + ensure login identities on every boot; full seed only when empty.

    Render free tier runs no pre-deploy/one-off jobs, so the app self-migrates.
    Three steps, deliberately gated differently:

    1. ``alembic upgrade head`` — ALWAYS. Idempotent (applies only pending
       revisions). Must run unconditionally: an already-populated live DB still
       needs newly-added migrations (e.g. the Users/org + BYOK tables). Gating
       this behind "DB empty" silently skipped them → ``users`` never existed →
       login 500'd.
    2. Login identities (orgs + password users + Google allowlist) — ALWAYS.
       Cheap + idempotent (no embedding). Must reach an EXISTING db too: the
       ``users`` table was added by a migration AFTER the original seed, so gating
       it on "DB empty" left the live db with a users table but no rows → login
       401'd. The env is the source of truth for the Google allowlist.
    3. Full seed (CRM + bots + knowledge base WITH embeddings) — only when empty.
       This is the one expensive step we must not repeat per boot.
    """
    from sqlalchemy import func, select

    from koel.db.models import BotConfig
    from koel.db.seed_users import seed_google_allowlist, seed_orgs, seed_users
    from koel.db.session import session_scope

    # 1) Schema → head. `python -m alembic` from the WORKDIR (which holds
    #    alembic.ini) applies every pending revision. Off the loop — env.py drives
    #    its own asyncio loop.
    logger.info("applying migrations (alembic upgrade head)")
    await asyncio.to_thread(
        subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "head"], check=True
    )

    # 2) Ensure login identities exist regardless of whether the DB is empty.
    logger.info("ensuring login identities (orgs + users + google allowlist)")
    async with session_scope() as db:
        await seed_orgs(db)
        await seed_users(db)
        await seed_google_allowlist(db, get_common_settings().google_allowlist)

    # 3) Expensive full seed only on a fresh DB (bots absent ⇒ nothing seeded yet).
    need = True
    try:
        async with session_scope() as db:
            count = (await db.execute(select(func.count()).select_from(BotConfig))).scalar_one()
            need = count == 0
    except Exception:  # noqa: BLE001 — defensive: treat any probe failure as "seed"
        need = True
    if not need:
        return
    logger.info("schema empty → seeding CRM + bots + knowledge base")
    # `python -m` puts CWD (the WORKDIR that holds scripts/) on sys.path; the
    # bootstrap script re-runs alembic upgrade head (no-op here) then seeds.
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
