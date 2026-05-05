"""Prometheus metrics. Module-level so they register exactly once.

Gateway-added latency and cache lookup latency are sub-millisecond on the
hot path; the Prometheus ``Histogram`` bucket spacing of 5–25 ms hides
that resolution. Both are now tracked by
:class:`pulseroute_shared.hdr.HdrHistogram` and exported on scrape via
``HdrHistogram.export_prometheus``. End-to-end request latency keeps the
existing Prometheus ``Histogram`` (provider-side, where 25 ms granularity
matches reality).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge
from pulseroute_shared.hdr import HdrHistogram

GATEWAY_ADDED_LATENCY_HDR = HdrHistogram()
"""Microsecond-resolution gateway-added latency, excludes upstream time."""

CACHE_LOOKUP_LATENCY_HDR = HdrHistogram()
"""Microsecond-resolution cache lookup latency (hit + miss paths)."""

CACHE_HIT_RATE = Gauge(
    "cache_hit_rate",
    "Rolling cache hit rate per tenant.",
    labelnames=("tenant_id",),
)

CACHE_LOOKUPS = Counter(
    "cache_lookups_total",
    "Cache lookup outcomes.",
    labelnames=("tenant_id", "outcome"),
)

COST_USD_TOTAL = Counter(
    "cost_usd_total",
    "Total estimated USD spend per (tenant, model).",
    labelnames=("tenant_id", "model"),
)

CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "0=closed, 1=half_open, 2=open.",
    labelnames=("provider", "model"),
)

PROVIDER_ERRORS = Counter(
    "provider_error_total",
    "Provider error count by code.",
    labelnames=("provider", "model", "code"),
)

PROVIDER_REQUESTS = Counter(
    "provider_requests_total",
    "Provider request count.",
    labelnames=("provider", "model"),
)


def record_gateway_added(elapsed_seconds: float) -> None:
    """Record gateway-added latency into the HDR histogram (microseconds)."""
    GATEWAY_ADDED_LATENCY_HDR.record(int(elapsed_seconds * 1_000_000))


def record_cache_lookup(elapsed_seconds: float) -> None:
    """Record cache lookup latency into the HDR histogram (microseconds)."""
    CACHE_LOOKUP_LATENCY_HDR.record(int(elapsed_seconds * 1_000_000))


def hdr_exposition() -> str:
    """Render the HDR-backed metric blocks for the /metrics scrape."""
    blocks: list[str] = []
    if GATEWAY_ADDED_LATENCY_HDR.total_count() > 0:
        blocks.append(
            GATEWAY_ADDED_LATENCY_HDR.export_prometheus(
                "gateway_added_latency_seconds",
                help_text="HDR-backed gateway-added latency (excludes upstream).",
            )
        )
    if CACHE_LOOKUP_LATENCY_HDR.total_count() > 0:
        blocks.append(
            CACHE_LOOKUP_LATENCY_HDR.export_prometheus(
                "cache_lookup_latency_seconds",
                help_text="HDR-backed cache lookup latency (hit + miss paths).",
            )
        )
    return "".join(blocks)
