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

from opentelemetry import metrics, trace
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
_meter_provider_set = False

# Metric instruments (created once per process in configure_metrics).
_turn_latency = None  # audio-in → audio-out, ms
_hop_latency = None  # per STT/NLP/TTS hop, ms
_errors = None  # pipeline errors by hop
_nlp_tokens = None  # Groq tokens consumed
_nlp_cost = None  # Groq cost, USD


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


def configure_metrics(service_name: str, settings: CommonSettings) -> None:
    """Wire the global meter provider + KOEL instruments once per process (Q).

    Exports over OTLP when an endpoint is configured (→ collector → Prometheus/
    Grafana); otherwise instruments still record to a no-op reader so call sites
    never branch on whether metrics are enabled.
    """
    global _meter_provider_set
    global _turn_latency, _hop_latency, _errors, _nlp_tokens, _nlp_cost

    if settings.otel_enabled and not _meter_provider_set:
        from opentelemetry.sdk.metrics import MeterProvider

        resource = Resource.create(
            {SERVICE_NAME: service_name, "deployment.environment": settings.environment}
        )
        readers = []
        if settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
                    )
                )
            )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
        _meter_provider_set = True

    meter = metrics.get_meter(service_name)
    _turn_latency = meter.create_histogram(
        "koel.turn.latency", unit="ms", description="End-to-end audio-in→audio-out turn latency"
    )
    _hop_latency = meter.create_histogram(
        "koel.hop.latency", unit="ms", description="Per-hop (stt/nlp/tts) latency"
    )
    _errors = meter.create_counter("koel.errors", description="Pipeline errors by hop")
    _nlp_tokens = meter.create_counter("koel.nlp.tokens", description="Groq tokens consumed")
    _nlp_cost = meter.create_counter(
        "koel.nlp.cost_usd", unit="USD", description="Estimated Groq spend"
    )


def record_turn_latency(ms: float) -> None:
    if _turn_latency is not None:
        _turn_latency.record(ms)


def record_hop_latency(hop: str, ms: float) -> None:
    if _hop_latency is not None:
        _hop_latency.record(ms, {"hop": hop})


def record_error(hop: str) -> None:
    if _errors is not None:
        _errors.add(1, {"hop": hop})


def record_nlp_usage(tokens: int, cost_usd: float) -> None:
    if _nlp_tokens is not None:
        _nlp_tokens.add(tokens)
    if _nlp_cost is not None:
        _nlp_cost.add(cost_usd)


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
