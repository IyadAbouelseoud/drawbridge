"""OpenTelemetry wiring — one tracer provider per process, and a trace id on the record.

Three consumers, and they want different things from this module.

The **API** wants request spans, which `opentelemetry-instrumentation-fastapi` produces
without our help once a provider exists. The **MCP servers** and the **agent worker** want
a span around the work they do on behalf of a request that started somewhere else. And the
**ledger** wants the trace id, so a row written four years ago can be tied back to the run
that wrote it — see `ledger.record`.

**The exporter is optional and the tracing is not.** `configure_tracing` builds a provider
whether or not an OTLP exporter is installed and whether or not a collector is reachable;
without one, spans are created, sampled and dropped. That is deliberate. The trace id ends
up in `audit_ledger` regardless, so the recordkeeping value does not depend on an
observability container being up — and an API that will not start because Jaeger is down
has traded a real dependency for an imaginary one.

The exporter import is therefore lazy and its absence is logged rather than raised. The
package is declared in `pyproject.toml` and is present in the images; it is not present in
every developer's environment, and the test suite must not require a collector.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer

if TYPE_CHECKING:
    from fastapi import FastAPI

log = structlog.get_logger()

ENDPOINT_ENV = "DRAWBRIDGE_OTEL_EXPORTER_ENDPOINT"

# Set once per process. Re-entrancy matters: the MCP servers import the API package, and
# a second `set_tracer_provider` is a warning and a silently ignored call.
_configured = False


def _exporter(endpoint: str) -> Any | None:
    """The OTLP/HTTP exporter, or None with a reason.

    HTTP rather than gRPC: one fewer transitive dependency, and Jaeger, Tempo and the
    collector all accept it on 4318.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        log.info("telemetry.exporter_unavailable", endpoint=endpoint)
        return None
    return OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")


def configure_tracing(service_name: str, *, endpoint: str | None = None) -> TracerProvider:
    """Install the process-wide provider. Idempotent."""
    # A process has one tracer provider; OTel ignores a second `set_tracer_provider`.
    global _configured

    provider = trace.get_tracer_provider()
    if _configured and isinstance(provider, TracerProvider):
        return provider

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "drawbridge",
            "deployment.environment": os.environ.get("DRAWBRIDGE_ENVIRONMENT", "development"),
        }
    )
    provider = TracerProvider(resource=resource)

    target = endpoint if endpoint is not None else os.environ.get(ENDPOINT_ENV, "").strip()
    if target:
        exporter = _exporter(target)
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
            log.info("telemetry.exporting", service=service_name, endpoint=target)
    else:
        # Not a warning. Spans still carry the trace id the ledger records, and the
        # local suite runs without a collector on purpose.
        log.debug("telemetry.no_exporter", service=service_name)

    trace.set_tracer_provider(provider)
    _configured = True
    return provider


def instrument_fastapi(app: FastAPI) -> None:
    """Attach the ASGI instrumentation, if it is installed.

    `/health` and `/ready` are excluded: they are polled by Docker every fifteen seconds
    per container and would be most of the trace volume while carrying none of the
    information.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        log.info("telemetry.fastapi_instrumentation_unavailable")
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready")


def tracer(name: str) -> Tracer:
    return trace.get_tracer(name)


def current_span() -> Span:
    return trace.get_current_span()


def current_trace_id() -> str | None:
    """The active trace as 32 lowercase hex characters, or None outside a trace.

    Returned as text rather than as the int OTel carries, because it is going into a
    database column an auditor reads and pastes into a trace UI.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid or context.trace_id == 0:
        return None
    return format(context.trace_id, "032x")


def shutdown_tracing() -> None:
    """Flush the batch processor. Called from the API's lifespan teardown.

    Without it the last few seconds of spans die with the process, which is exactly the
    window containing whatever made someone restart it.
    """
    global _configured  # paired with configure_tracing

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()
    _configured = False
