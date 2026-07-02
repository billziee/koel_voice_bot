"""Shared configuration.

Every setting is environment-driven (12-factor) so the same image runs in dev,
CI, and the demo box without code changes. Service-specific settings subclass
``CommonSettings``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- PostgreSQL + pgvector ---
    database_url: str = "postgresql+asyncpg://koel:koel@localhost:5432/koel"
    # Connection pool (Postgres only; PRD §14 stability under load). Sized for a
    # per-service uvicorn worker — tune with worker count against RDS max_connections.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_recycle_s: int = 1800  # recycle before RDS/proxy idle timeout
    db_pool_timeout_s: int = 30

    # --- Call recording + retention (K, PRD §11 audit / 30-day retention) ---
    s3_recording_bucket: str = ""  # empty = recording disabled (dev); set in prod
    s3_region: str = "ap-south-1"
    recording_retention_days: int = 30

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
    keepalive_interval_seconds: int = 600  # 10 min, comfortably under the 15-min cutoff

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_common_settings() -> CommonSettings:
    return CommonSettings()
