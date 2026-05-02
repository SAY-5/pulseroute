"""OpenTelemetry bootstrap. Safe to import without an exporter configured;
falls back to a no-op tracer so unit tests do not need a collector."""

from __future__ import annotations

import logging
import os
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialised = False


def init_tracing(service_name: str = "pulseroute-gateway") -> None:
    """Initialise tracing once. Idempotent."""
    global _initialised
    if _initialised:
        return
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        except Exception as exc:  # pragma: no cover - exporter missing in tests
            logging.getLogger(__name__).warning("otel exporter unavailable: %s", exc)

    trace.set_tracer_provider(provider)
    _initialised = True


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def bind_request_context(**kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
