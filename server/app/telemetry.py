"""OpenTelemetry wiring.

Exports to Application Insights when APPLICATIONINSIGHTS_CONNECTION_STRING is
present (Foundry injects it into hosted containers); otherwise falls back to a
console exporter so local runs still show spans.
"""
from __future__ import annotations

import logging

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
    resource = Resource.create(
        {
            "service.name": FOUNDRY_AGENT_NAME,
            "service.version": FOUNDRY_AGENT_VERSION,
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


def tracer():
    return trace.get_tracer("ghcp-sdk-browser")
