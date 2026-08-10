"""OpenTelemetry wiring.

Exports to Application Insights when a connection string can be resolved;
otherwise falls back to a console exporter so local runs still show spans.

Why the resolution is more than one env var
-------------------------------------------
The hosted platform does NOT reliably inject
``APPLICATIONINSIGHTS_CONNECTION_STRING`` into the agent container, and the
console fallback is silent -- so spans are emitted, look fine in logs, and
never reach Foundry. That failure mode is indistinguishable from "tracing is
broken", so this module now (a) tries several sources and (b) exposes
``telemetry_status()`` so /info can report which one won.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .config import FOUNDRY_AGENT_NAME, FOUNDRY_AGENT_VERSION

log = logging.getLogger("telemetry")
_ready = False
#: Populated by setup_tracing so /info can report where spans are going.
_status: dict[str, object] = {"exporter": "not-initialised", "source": None}


def _connection_string() -> tuple[str, str]:
    """Resolve an App Insights connection string. Returns (value, source).

    Read lazily rather than at import so a value injected later in startup is
    still picked up, and tried in order of trustworthiness.
    """
    for var in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "APPINSIGHTS_CONNECTION_STRING",
        "AZURE_MONITOR_CONNECTION_STRING",
        # azd writes this one when the service `uses:` a project with
        # Application Insights attached.
        "APPLICATIONINSIGHTS_CONNECTIONSTRING",
    ):
        value = os.getenv(var, "").strip()
        if value:
            return value, var
    return "", ""


def telemetry_status() -> dict[str, object]:
    """Where spans are actually going. Surfaced on /info for diagnosis."""
    return dict(_status)


def setup_tracing() -> None:
    global _ready
    if _ready:
        return

    # Foundry's agent view only renders spans that follow the OTel GenAI
    # semantic conventions, and the SDK/exporter gate the experimental
    # gen_ai.* attributes behind these opt-ins. They must be set BEFORE the
    # provider is built, so they are applied here rather than in the
    # Dockerfile where a local run would miss them.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    os.environ.setdefault("AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED", "true")

    resource = Resource.create(
        {
            "service.name": FOUNDRY_AGENT_NAME,
            "service.version": FOUNDRY_AGENT_VERSION,
            # Foundry correlates traces to the registered agent via these.
            "gen_ai.agent.name": FOUNDRY_AGENT_NAME,
            "gen_ai.agent.id": FOUNDRY_AGENT_NAME,
            "gen_ai.system": "github.copilot",
        }
    )
    provider = TracerProvider(resource=resource)

    conn, source = _connection_string()
    if conn:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

            provider.add_span_processor(
                BatchSpanProcessor(AzureMonitorTraceExporter.from_connection_string(conn))
            )
            _status.update(exporter="application-insights", source=source)
            log.info("tracing -> Application Insights (via %s)", source)
        except Exception as exc:  # noqa: BLE001 - never fail startup on telemetry
            log.warning("App Insights exporter unavailable (%s); using console", exc)
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            _status.update(exporter="console", source=source, error=str(exc))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        _status.update(
            exporter="console",
            source=None,
            error="no App Insights connection string in the environment",
        )
        log.warning(
            "tracing -> console: no App Insights connection string found. "
            "Spans will NOT appear in Foundry."
        )

    trace.set_tracer_provider(provider)
    _ready = True

    # Foundry discovers a custom agent by looking for a span with
    # gen_ai.operation.name="create_agent" carrying the agent id. Without one
    # the portal has nothing to attach the invoke_agent/execute_tool spans to.
    with tracer().start_as_current_span(f"create_agent {FOUNDRY_AGENT_NAME}") as span:
        span.set_attribute("gen_ai.operation.name", "create_agent")
        span.set_attribute("gen_ai.agent.name", FOUNDRY_AGENT_NAME)
        span.set_attribute("gen_ai.agent.id", FOUNDRY_AGENT_NAME)
        span.set_attribute("gen_ai.system", "github.copilot")


def flush() -> None:
    """Force-export buffered spans.

    BatchSpanProcessor holds spans for up to 5s. A turn that ends right before
    the container is scaled down would otherwise lose its trace, which looks
    like missing instrumentation.
    """
    try:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception:  # noqa: BLE001 - flushing must never break a turn
        log.debug("span flush failed", exc_info=True)


def tracer():
    return trace.get_tracer("ghcp-sdk-browser")


# ---------------------------------------------------------------------------
# Trace-context propagation
# ---------------------------------------------------------------------------
# Foundry roots its Traces view on the platform's `invoke_agent` request
# (cloud_RoleName=agentsv2). A child span appears there only if it shares that
# request's trace id. Two things previously broke that:
#
#   1. The inbound W3C `traceparent` header was never extracted, so every span
#      began a brand-new trace.
#   2. A turn runs in a detached asyncio task (start_turn creates one so the
#      HTTP response can return 202), and OTel context does NOT cross that
#      boundary -- so even our own tool spans did not nest under our turn span.
#
# Measured before the fix: 0 shared operation_Ids between the platform's spans
# and ours, and 14 distinct trace ids for what should have been ~3 traces.

#: relay session id -> the platform trace context for that session.
_SESSION_CONTEXT: dict[str, Context] = {}


def remember_trace_context(session_id: str, headers: Mapping[str, str]) -> None:
    """Extract the inbound traceparent and pin it to this session.

    Stored per session because the turn is executed later, on a different
    task, once the originating request has already returned.
    """
    try:
        carrier = {k.lower(): v for k, v in headers.items()}
        if "traceparent" not in carrier:
            return
        ctx = TraceContextTextMapPropagator().extract(carrier=carrier)
        if trace.get_current_span(ctx).get_span_context().is_valid:
            _SESSION_CONTEXT[session_id] = ctx
    except Exception:  # noqa: BLE001 - propagation must never break a request
        log.debug("could not extract trace context", exc_info=True)


def session_context(session_id: str) -> Context | None:
    """The platform trace context for a session, if we captured one."""
    return _SESSION_CONTEXT.get(session_id)


def forget_trace_context(session_id: str) -> None:
    _SESSION_CONTEXT.pop(session_id, None)

