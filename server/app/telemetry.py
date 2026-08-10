"""OpenTelemetry wiring.

Exports to Application Insights when APPLICATIONINSIGHTS_CONNECTION_STRING is
present (Foundry injects it into hosted containers); otherwise falls back to a
console exporter so local runs still show spans.
"""
from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from .config import APPINSIGHTS, FOUNDRY_AGENT_NAME, FOUNDRY_AGENT_VERSION

log = logging.getLogger("telemetry")
_ready = False


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

    if APPINSIGHTS:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

            provider.add_span_processor(
                BatchSpanProcessor(
                    AzureMonitorTraceExporter.from_connection_string(APPINSIGHTS)
                )
            )
            log.info("tracing -> Application Insights")
        except Exception as exc:  # noqa: BLE001 - never fail startup on telemetry
            log.warning("App Insights exporter unavailable (%s); using console", exc)
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        log.info("tracing -> console (no APPLICATIONINSIGHTS_CONNECTION_STRING)")

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


def tracer():
    return trace.get_tracer("ghcp-sdk-browser")
