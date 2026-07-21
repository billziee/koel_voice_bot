"""Shared singletons for VB_Svc, created at startup and reused across requests.

Held on ``app.state`` (set in the lifespan) so there is no module-level global
state that complicates testing. Accessors raise if used before startup.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import redis.asyncio as redis
from fastapi import Request

from koel.voicebot.clients import ServiceClients
from koel.voicebot.config import VoiceBotSettings
from koel.voicebot.escalation import EscalationHub
from koel.voicebot.monitor import LiveMonitor
from koel.voicebot.pipeline.bargein import BargeInClassifier
from koel.voicebot.pipeline.endpointing import SmartTurnAnalyzer
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
    # Semantic end-of-turn gate (None unless KOEL_SMART_TURN_ENABLED).
    smart_turn: SmartTurnAnalyzer | None = None
    # Live operator↔caller bridge for human handoff (3.1). Shared across the WS
    # handler (caller side) and the operator REST API (operator side).
    escalations: EscalationHub = field(default_factory=EscalationHub)
    # Read-only live view of in-flight calls for supervisor oversight (3.3).
    monitor: LiveMonitor = field(default_factory=LiveMonitor)
    # Strong refs to detached grace-window finalizers (PRD §05 dropped calls),
    # so they survive after the WebSocket handler returns.
    background_tasks: set[asyncio.Task] = field(default_factory=set)
    # The raw client backing escalations/monitor (None in tests, which use the
    # in-process default) — kept so the lifespan can close it on shutdown.
    shared_redis: redis.Redis | None = None


def build_runtime(settings: VoiceBotSettings) -> Runtime:
    store = RedisSessionStore(
        settings.redis_url,
        active_ttl=settings.session_active_ttl_seconds,
        grace_ttl=settings.session_grace_ttl_seconds,
    )
    clients = ServiceClients(settings)
    smart_turn = None
    if settings.smart_turn_enabled:
        smart_turn = SmartTurnAnalyzer(
            model_path=settings.smart_turn_model_path,
            threshold=settings.smart_turn_threshold,
        )
    bargein = BargeInClassifier(
        backchannel_max_speech_ms=settings.backchannel_max_speech_ms,
        min_interrupt_words=settings.min_interrupt_words,
        single_word_interrupt_ms=settings.single_word_interrupt_ms,
    )
    filler = FillerCache()
    orchestrator = Orchestrator(settings=settings, clients=clients, filler_cache=filler)
    # Redis-backed monitor/escalation (a follow-up noted in docs/FUTURE_PROCEEDINGS.md,
    # now implemented): a separate connection from the session store's own, since
    # RedisSessionStore doesn't expose its client and the pool cost is negligible.
    # Redis is already required infra for this app (the session store has no
    # in-process fallback), so this is unconditional, not dormant-until-configured.
    monitor_redis = redis.from_url(settings.redis_url, decode_responses=True)
    monitor = LiveMonitor(redis_client=monitor_redis)
    escalations = EscalationHub(redis_client=monitor_redis, liveness=store)
    return Runtime(
        settings=settings,
        store=store,
        clients=clients,
        bargein=bargein,
        smart_turn=smart_turn,
        filler=filler,
        orchestrator=orchestrator,
        escalations=escalations,
        monitor=monitor,
        shared_redis=monitor_redis,
    )


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - defensive
        raise RuntimeError("Runtime not initialised; check the app lifespan.")
    return runtime
