"""Shared configuration.

Every setting is environment-driven (12-factor) so the same image runs in dev,
CI, and the demo box without code changes. Service-specific settings subclass
``CommonSettings``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 5 min: short enough that even one FAILED ping (loop waits ~one more interval
# before the next attempt) still lands two successful pings inside the ~15-min
# idle cutoff, but long enough not to hammer /health.
_DEFAULT_KEEPALIVE_INTERVAL_S = 300


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Environment ---
    environment: str = "development"  # development | staging | production
    debug: bool = True

    # --- Data residency (PRD §11: AWS ap-south-1) ---
    aws_region: str = "ap-south-1"

    # --- Logging (PRD §08 log-level discipline) ---
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    log_json: bool = True  # structured JSON logs for Kibana/ES ingestion

    # --- OpenTelemetry (PRD §08 distributed tracing) ---
    otel_enabled: bool = True
    otel_exporter_otlp_endpoint: str | None = None  # e.g. http://otel-collector:4317
    otel_console_export: bool = False  # dump spans to stdout in dev
    # FastAPI inbound-HTTP auto-instrumentation. Off by default: the current
    # instrumentor errors on unmatched routes (e.g. CORS preflight OPTIONS).
    # Manual per-hop spans (koel.common.telemetry.span) provide the latency
    # tracing the <300ms budget needs regardless. Enable with a fixed lib.
    otel_instrument_fastapi: bool = False

    # --- Auth (PRD §08: JWT on REST + WebSocket) ---
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    # Short access TTL (M): the dashboard silently refreshes via /v1/auth/refresh,
    # so a leaked token has a small blast radius.
    jwt_access_ttl_seconds: int = 900
    # Short-lived token minted for the WebSocket upgrade specifically.
    jwt_ws_ttl_seconds: int = 120

    # --- CORS: the React dashboard origin(s) allowed to call the API ---
    # Comma-separated, or "*" in dev. Bearer-token auth (not cookies) so "*" is safe.
    cors_allow_origins: str = "*"

    # --- Redis (PRD §08: all session state in Redis with TTL) ---
    redis_url: str = "redis://localhost:6379/0"
    # Rate-limiter backend (O-8): "memory" is per-process (fine for a single
    # worker / dev / tests); "redis" shares the window across workers + instances
    # so limits don't multiply by worker count. Enable in multi-worker deploys.
    rate_limit_backend: str = "memory"

    # --- PostgreSQL + pgvector ---
    database_url: str = "postgresql+asyncpg://koel:koel@localhost:5432/koel"
    # Connection pool (Postgres only; PRD §14 stability under load). Sizing math
    # (validate with scripts/loadtest_pool.py — see docs/LOADTEST.md): the peak
    # live connections a single service instance can open is
    # ``(db_pool_size + db_max_overflow) * uvicorn_workers``. That product, summed
    # across every service instance, MUST stay under RDS ``max_connections`` (minus
    # a headroom margin for migrations/psql/other clients). Defaults 10+5=15 give a
    # 1-worker instance 15 peak connections; e.g. 4 services × 1 worker = 60, well
    # under a t3 RDS's ~80–100 cap. Each request holds a connection only for the
    # brief ``session_scope`` around its query (not the whole request), so 10 steady
    # + 5 burst comfortably covers the PRD's 10-concurrent-session target. Bump
    # db_pool_size when koel.db.pool.in_use sits at the ceiling and latency shows
    # checkout-wait; raise RDS max_connections (or add pgbouncer) before scaling
    # workers × instances past the connection budget.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_recycle_s: int = 1800  # recycle before RDS/proxy idle timeout
    # Seconds a request waits for a free connection before erroring (SQLAlchemy
    # ``pool_timeout``). 30s is SQLAlchemy's default and is intentionally kept: it
    # is a last-resort backstop, not a latency knob — a real-time turn is long dead
    # before 30s, so under genuine pool exhaustion the useful signal is the rising
    # koel.db.pool.in_use + checkout-wait tail (alert on those), and a shorter
    # timeout would only convert brief bursts into user-facing 5xx. Lower it for a
    # deployment that would rather fail-fast than queue; it is env-overridable.
    db_pool_timeout_s: int = 30
    # LIFO connection checkout (SQLAlchemy ``pool_use_lifo``). LIFO reuses the
    # most-recently-returned connection, so during non-peak periods the pool's
    # excess connections stay idle and age out via ``db_pool_recycle_s`` /
    # server-side idle timeouts instead of being round-robined and kept alive.
    # This shrinks the live-connection footprint against RDS max_connections
    # between bursts. Safe here because ``pool_pre_ping`` (get_engine) already
    # reaps any connection the server closed underneath us. Default on; set
    # KOEL_DB_POOL_USE_LIFO=false to restore FIFO round-robin behaviour.
    db_pool_use_lifo: bool = True
    # pgvector HNSW query-time recall/latency knob (roadmap O-10). ``hnsw.ef_search``
    # sizes the candidate list the index walks per query: higher ⇒ better recall,
    # slower; lower ⇒ faster, may miss neighbours. 0 leaves the Postgres server
    # default (typically 40) untouched; >0 makes RAG retrieve() issue a transaction-
    # scoped ``SET LOCAL hnsw.ef_search`` before the nearest-neighbour query, so
    # recall/latency can be validated at target concurrency without a server restart.
    pgvector_hnsw_ef_search: int = 0

    # --- Call recording + retention (K, PRD §11 audit / 30-day retention) ---
    s3_recording_bucket: str = ""  # empty = recording disabled (dev); set in prod
    s3_region: str = "ap-south-1"
    recording_retention_days: int = 30

    # --- TTS clip cache retention (O-3, DPDP §11 data-minimisation) ---
    # The TTS caches key on the RENDERED spoken line, which can embed a customer's
    # CRM name (e.g. "Rahul ji, ..."), so a cached clip and its text are derived
    # personal data. A short TTL bounds how long that PII can sit in process memory
    # cross-session; a process restart clears it entirely. Kept short by default
    # (30 min, not hours) so retention stays minimal, trading a little hit-rate on
    # rarely-repeated lines for a tighter window. ``tts_cache_max_bytes`` is the
    # rough per-cache memory cap enforced by LRU eviction.
    tts_cache_ttl_s: int = 1800  # 30 min
    tts_cache_max_bytes: int = 64 * 1024 * 1024

    # --- Inter-service base URLs (the orchestrator fans out to these) ---
    stt_svc_url: str = "http://localhost:8101"
    nlp_svc_url: str = "http://localhost:8102"
    tts_svc_url: str = "http://localhost:8103"
    crm_svc_url: str = "http://localhost:8104"

    # --- Keep-alive self-ping (free-tier hosts spin down when idle) ---
    # A free-tier host (e.g. Render) spins the instance down after ~15 min with no
    # INBOUND traffic (~50s cold start on the next request). Set this to the app's
    # own PUBLIC url and the single-process combined app pings <url>/health on an
    # interval, generating that inbound traffic so it stays warm between demos.
    # Unset ⇒ no task, so it never runs locally or in CI. This only KEEPS a running
    # instance warm; it can't wake one already spun down — the CI cron does that.
    keepalive_url: str | None = None
    keepalive_interval_seconds: int = _DEFAULT_KEEPALIVE_INTERVAL_S

    @field_validator("keepalive_interval_seconds")
    @classmethod
    def _sane_keepalive_interval(cls, v: int) -> int:
        # A non-positive interval would turn the ping loop into a no-delay busy
        # loop (asyncio.sleep(0) never yields real time). Coerce to the default
        # rather than crash the whole app on a keep-alive misconfig.
        return v if v > 0 else _DEFAULT_KEEPALIVE_INTERVAL_S

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_common_settings() -> CommonSettings:
    return CommonSettings()
