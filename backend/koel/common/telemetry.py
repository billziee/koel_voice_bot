"""OpenTelemetry setup.

PRD §08: distributed traces spanning FE → VB_Svc → AI microservices, every
request carrying a trace ID, latency measured at each hop so the <300ms turn
budget (PRD §05) is attributable per service.

Trace context propagates over HTTP via explicit W3C inject/extract: outbound
fan-out calls inject ``traceparent`` (koel.voicebot.clients) and each service's
correlation middleware extracts it (koel.common.app_factory), so the manual
``span()`` hops below stitch into one distributed trace across FE → VB_Svc → AI.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, Tracer

from koel.common.config import CommonSettings

_tracer: Tracer | None = None
_provider_set = False


def configure_telemetry(service_name: str, settings: CommonSettings) -> None:
    """Wire the global tracer provider once per service process (idempotent)."""
    global _tracer, _provider_set
    if not settings.otel_enabled:
        _tracer = trace.get_tracer(service_name)
        return
    if _provider_set:
        # A provider is already installed (e.g. multiple apps in one process);
        # reuse it rather than triggering an override warning.
        _tracer = trace.get_tracer(service_name)
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            "deployment.environment": settings.environment,
            "cloud.region": settings.aws_region,
        }
    )
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        # Import lazily so the OTLP exporter dependency is optional in dev.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
            )
        )
    if settings.otel_console_export:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _provider_set = True
    _tracer = trace.get_tracer(service_name)


def get_tracer() -> Tracer:
    if _tracer is None:
        # Safe default before configure_telemetry runs (e.g. in unit tests).
        return trace.get_tracer("koel.uninitialized")
    return _tracer


@contextmanager
def span(name: str, **attributes: object) -> Iterator[Span]:
    """Open a span for one pipeline hop. Records exceptions and sets the error status."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)  # type: ignore[arg-type]
        try:
            yield current
        except Exception as exc:  # noqa: BLE001 — re-raised after recording
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise


def current_trace_id() -> str | None:
    """Hex trace id of the active span, for use as a correlation id."""
    ctx = trace.get_current_span().get_span_context()
    if ctx and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return None
