"""End-to-end check: HDR-backed gateway metrics flow through /metrics."""

from __future__ import annotations

import pytest
from pulseroute_gateway.metrics import (
    CACHE_LOOKUP_LATENCY_HDR,
    GATEWAY_ADDED_LATENCY_HDR,
)


@pytest.mark.asyncio
async def test_chat_completion_populates_hdr(app_client) -> None:
    GATEWAY_ADDED_LATENCY_HDR.reset()
    CACHE_LOOKUP_LATENCY_HDR.reset()

    for _ in range(3):
        resp = await app_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer pr_test_quality"},
            json={
                "model": "fake-large",
                "messages": [{"role": "user", "content": "hello world"}],
            },
        )
        assert resp.status_code == 200

    # Both HDR histograms should have observations.
    assert GATEWAY_ADDED_LATENCY_HDR.total_count() >= 3
    assert CACHE_LOOKUP_LATENCY_HDR.total_count() >= 3


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_hdr_blocks(app_client) -> None:
    GATEWAY_ADDED_LATENCY_HDR.reset()
    CACHE_LOOKUP_LATENCY_HDR.reset()

    # Drive a couple of requests so both HDR histograms have entries.
    for content in ("alpha bravo", "charlie delta"):
        resp = await app_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer pr_test_quality"},
            json={
                "model": "fake-large",
                "messages": [{"role": "user", "content": content}],
            },
        )
        assert resp.status_code == 200

    metrics = await app_client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text
    # HDR-backed exposition is appended after standard Prometheus output.
    assert "gateway_added_latency_seconds_bucket" in body
    assert "gateway_added_latency_seconds_count" in body
    assert "cache_lookup_latency_seconds_bucket" in body
    assert "cache_lookup_latency_seconds_count" in body
    # Existing Prometheus counters survive.
    assert "cache_lookups_total" in body
