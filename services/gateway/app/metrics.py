"""Prometheus metrics. Module-level so they register exactly once."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

GATEWAY_ADDED_LATENCY = Histogram(
    "gateway_added_latency_seconds",
    "Latency added by the gateway, excluding upstream provider time.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

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
