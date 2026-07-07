"""Shared singletons for VB_Svc, created at startup and reused across requests.

Held on ``app.state`` (set in the lifespan) so there is no module-level global
state that complicates testing. Accessors raise if used before startup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import Request

from koel.voicebot.clients import ServiceClients
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.filler import FillerCache
from koel.voicebot.pipeline.orchestrator import Orchestrator
from koel.voicebot.session.store import RedisSessionStore


@dataclass
class Runtime:
    settings: VoiceBotSettings
    store: RedisSessionStore
    clients: ServiceClients
    bargein: BargeInClassifier
    filler: FillerCache
    orchestrator: Orchestrator
    # Strong refs to detached grace-window finalizers (PRD §05 dropped calls),
    # so they survive after the WebSocket handler returns.
    background_tasks: set[asyncio.Task] = field(default_factory=set)


def build_runtime(settings: VoiceBotSettings) -> Runtime:
    store = RedisSessionStore(
        settings.redis_url,
        active_ttl=settings.session_active_ttl_seconds,
        grace_ttl=settings.session_grace_ttl_seconds,
    )
    clients = ServiceClients(settings)
    bargein = BargeInClassifier(
        backchannel_max_speech_ms=settings.backchannel_max_speech_ms,
        min_interrupt_words=settings.min_interrupt_words,
        single_word_interrupt_ms=settings.single_word_interrupt_ms,
    )
    filler = FillerCache()
    orchestrator = Orchestrator(settings=settings, clients=clients, filler_cache=filler)
    return Runtime(
        settings=settings,
        store=store,
        clients=clients,
        bargein=bargein,
        filler=filler,
        orchestrator=orchestrator,
    )


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - defensive
        raise RuntimeError("Runtime not initialised; check the app lifespan.")
    return runtime
