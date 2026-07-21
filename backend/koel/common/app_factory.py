"""``create_app`` — the single entry point every service builds on.

Wiring it once here guarantees the PRD §08 standards hold uniformly: OpenAPI at
``/docs``, OTel instrumentation, structured logging + per-request correlation
ids, the shared error envelope, and a ``/health`` endpoint. New services inherit
all of it for free — "observable by default, configurable by design" (PRD §07).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import extract

from koel import __version__
from koel.common.config import CommonSettings, get_common_settings
from koel.common.crypto import secrets_available
from koel.common.errors import register_exception_handlers
from koel.common.logging import configure_logging, get_logger, set_correlation_id
from koel.common.schemas import HealthResponse, HealthStatus
from koel.common.telemetry import configure_metrics, configure_telemetry

CORRELATION_HEADER = "x-correlation-id"

# Swagger/ReDoc load assets from a CDN, so a strict CSP would break them; skip CSP
# on those paths only (the rest of the headers still apply everywhere).
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def _apply_security_headers(response: Response, path: str, settings: CommonSettings) -> None:
    """OWASP baseline response headers (PRD §08 / web security rules)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "microphone=(self), camera=(), geolocation=()"
    )
    if settings.is_production:  # HSTS only makes sense over HTTPS
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    if not path.startswith(_DOCS_PATHS):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )


def warn_if_byok_secret_unset(settings: CommonSettings, logger) -> bool:  # noqa: ANN001 - stdlib Logger
    """BYOK model connections (koel.common.crypto) are legitimately optional —
    NOT a hard boot fail like the JWT/CORS checks in create_app — but silently
    dormant with no signal is its own footgun (docs/SECURITY.md: "easy to miss
    since BYOK fails closed silently"). One clear line at boot instead.
    Returns whether it warned (for tests), mirroring
    ``koel.voicebot.main.warn_if_turnstile_unenforced``."""
    if settings.is_production and not secrets_available():
        logger.warning(
            "byok_secret_key_unset_in_production: KOEL_SECRET_KEY is not set (or "
            "invalid) — BYOK model-connection creation/use will fail closed. Set "
            "it from a secret manager if BYOK is needed; harmless to ignore "
            "otherwise (env-configured providers are unaffected)."
        )
        return True
    return False


def create_app(
    *,
    service_name: str,
    title: str,
    description: str = "",
    settings: CommonSettings | None = None,
    lifespan: Callable[[FastAPI], object] | None = None,
    readiness_check: Callable[[], object] | None = None,
) -> FastAPI:
    settings = settings or get_common_settings()
    # Fail fast: never run in production with the insecure default JWT secret
    # (HS256 is symmetric — a known secret lets anyone forge access + ws tokens).
    if settings.is_production and settings.jwt_secret == CommonSettings.model_fields["jwt_secret"].default:
        raise RuntimeError(
            "KOEL_JWT_SECRET must be set to a strong value in production "
            "(the default dev secret is insecure)."
        )
    # In production, CORS must be an explicit allowlist — never '*' or empty
    # (Bearer auth mitigates but wildcard origins still widen the attack surface).
    if settings.is_production:
        cors = settings.cors_allow_origins.strip()
        if not cors or cors == "*":
            raise RuntimeError(
                "KOEL_CORS_ALLOW_ORIGINS must be an explicit comma-separated allowlist "
                "in production (not '*' or empty)."
            )
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    configure_telemetry(service_name, settings)
    configure_metrics(service_name, settings)
    logger = get_logger(f"koel.{service_name}")
    warn_if_byok_secret_unset(settings, logger)

    @asynccontextmanager
    async def _default_lifespan(_: FastAPI):
        logger.info("%s starting (env=%s)", service_name, settings.environment)
        yield
        logger.info("%s shutting down", service_name)

    app = FastAPI(
        title=title,
        version=__version__,
        description=description,
        # FastAPI's lifespan param is typed invariantly over the yielded state
        # type; our custom lifespans yield None but the annotation doesn't line up.
        # Targeted ignore (not a module-wide silence) — behaviourally correct.
        lifespan=lifespan or _default_lifespan,  # type: ignore[arg-type]
        # Richardson L2: proper docs surface per service (PRD §08).
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.service_name = service_name
    app.state.settings = settings

    register_exception_handlers(app)

    @app.middleware("http")
    async def _correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour an inbound correlation id (FE → VB_Svc → AI svc) or mint one.
        cid = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        set_correlation_id(cid)
        # Attach any inbound W3C trace context so this service's manual spans
        # become children of the caller's trace — one stitched distributed
        # trace across FE → VB_Svc → AI svc (PRD §08).
        token = otel_context.attach(extract(dict(request.headers)))
        try:
            response = await call_next(request)
        finally:
            otel_context.detach(token)
        response.headers[CORRELATION_HEADER] = cid
        _apply_security_headers(response, request.url.path, settings)
        return response

    def _health(health_status: HealthStatus, deps: dict[str, bool] | None = None) -> HealthResponse:
        return HealthResponse(
            service=service_name,
            status=health_status,
            version=__version__,
            environment=settings.environment,
            dependencies=deps or {},
        )

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    @app.get("/health/live", response_model=HealthResponse, tags=["meta"])
    async def health_live() -> HealthResponse:
        """Liveness (P): the process is up. Never touches dependencies — a slow DB
        must not cause the orchestrator to restart the pod."""
        return _health(HealthStatus.ok)

    @app.get("/health/ready", response_model=HealthResponse, tags=["meta"])
    async def health_ready(response: Response) -> HealthResponse:
        """Readiness (P): dependencies (DB/Redis/peers) reachable. 503 when not,
        so the load balancer stops routing to an unready replica."""
        if readiness_check is None:
            return _health(HealthStatus.ok)
        try:
            deps = await readiness_check()  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — any probe failure ⇒ not ready
            logger.warning("readiness_check_failed", exc_info=True)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return _health(HealthStatus.down, {})
        if not all(deps.values()):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return _health(HealthStatus.degraded, deps)
        return _health(HealthStatus.ok, deps)

    # Trace inbound HTTP automatically; excludes /health to keep traces signal-rich.
    if settings.otel_enabled and settings.otel_instrument_fastapi:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")

    # CORS must be the OUTERMOST middleware so it answers preflight OPTIONS before
    # routing/instrumentation runs (the OTel instrumentor errors on unmatched
    # routes). add_middleware prepends, so adding it last makes it outermost.
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    if not origins and settings.is_production:
        # A production value that parses to nothing (empty, or only separators like
        # ",,") is a misconfiguration — do NOT silently fall back to a wildcard.
        # Fail closed: no cross-origin until origins are configured. (Set the value
        # to a literal "*" to opt into wildcard — safe here since auth is bearer-token,
        # not cookies.)
        logger.error("cors_allow_origins is empty in production; refusing '*' fallback")
        allow_origins = origins
    else:
        allow_origins = origins or ["*"]  # dev convenience / explicit "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER],
    )

    return app
