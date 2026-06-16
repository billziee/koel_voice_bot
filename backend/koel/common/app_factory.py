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

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from koel import __version__
from koel.common.config import CommonSettings, get_common_settings
from koel.common.errors import register_exception_handlers
from koel.common.logging import configure_logging, get_logger, set_correlation_id
from koel.common.schemas import HealthResponse, HealthStatus
from koel.common.telemetry import configure_telemetry

CORRELATION_HEADER = "x-correlation-id"


def create_app(
    *,
    service_name: str,
    title: str,
    description: str = "",
    settings: CommonSettings | None = None,
    lifespan: Callable[[FastAPI], object] | None = None,
) -> FastAPI:
    settings = settings or get_common_settings()
    # Fail fast: never run in production with the insecure default JWT secret
    # (HS256 is symmetric — a known secret lets anyone forge access + ws tokens).
    if settings.is_production and settings.jwt_secret == CommonSettings.model_fields["jwt_secret"].default:
        raise RuntimeError(
            "KOEL_JWT_SECRET must be set to a strong value in production "
            "(the default dev secret is insecure)."
        )
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    configure_telemetry(service_name, settings)
    logger = get_logger(f"koel.{service_name}")

    @asynccontextmanager
    async def _default_lifespan(_: FastAPI):
        logger.info("%s starting (env=%s)", service_name, settings.environment)
        yield
        logger.info("%s shutting down", service_name)

    app = FastAPI(
        title=title,
        version=__version__,
        description=description,
        lifespan=lifespan or _default_lifespan,
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
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        return HealthResponse(
            service=service_name,
            status=HealthStatus.ok,
            version=__version__,
            environment=settings.environment,
        )

    # Trace inbound HTTP automatically; excludes /health to keep traces signal-rich.
    if settings.otel_enabled and settings.otel_instrument_fastapi:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")

    # CORS must be the OUTERMOST middleware so it answers preflight OPTIONS before
    # routing/instrumentation runs (the OTel instrumentor errors on unmatched
    # routes). add_middleware prepends, so adding it last makes it outermost.
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER],
    )

    return app
