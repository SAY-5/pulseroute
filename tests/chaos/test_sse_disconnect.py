"""SSE disconnect chaos test.

Wires a chaos provider that yields N tokens then raises a connection error
(``httpx.ReadError`` analogue). Asserts the gateway emits:
  1. SSE chunks for the first N tokens
  2. A final SSE chunk with structured ``error`` field whose ``code`` is
     ``upstream_disconnected`` and ``retryable`` is True
  3. The terminal ``data: [DONE]`` line

Also asserts the partial response is handed to the persistence stub with
``error_code='upstream_disconnected'`` so a real ClickHouse writer can land
the row. (The stub stands in for the production ClickHouse insert; see
``ARCHITECTURE.md`` §sse-error-event-protocol.)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pulseroute_cache import HashEmbedder, SemanticCache
from pulseroute_gateway import streaming as streaming_module
from pulseroute_gateway.deps import Dependencies
from pulseroute_gateway.main import create_app
from pulseroute_router import (
    CheapestFirst,
    CostCapped,
    LatencyFirst,
    QualityFirst,
    Router,
)
from pulseroute_router.provider import ProviderResponse
from pulseroute_router.providers.fake import FakeProvider
from pulseroute_shared.settings import Settings
from pulseroute_shared.types import ChatCompletionRequest, ProviderName


@dataclass
class _ChaosStreamProvider:
    """A provider that streams ``n_tokens`` deltas, then raises
    ``httpx.ReadError`` mid-stream."""

    n_tokens: int = 3
    name: ProviderName = ProviderName.FAKE
    supported_models: frozenset[str] = field(
        default_factory=lambda: frozenset({"fake-small", "fake-large"})
    )

    async def complete(
        self, request: ChatCompletionRequest
    ) -> ProviderResponse:  # pragma: no cover
        return ProviderResponse(
            content="unreachable", prompt_tokens=0, completion_tokens=0, raw_model=request.model
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[str]:  # noqa: ARG002
        for i in range(self.n_tokens):
            yield f"tok{i}"
        raise httpx.ReadError("simulated upstream disconnect")

    async def healthcheck(self) -> bool:
        return True


@dataclass
class _PersistenceStub:
    """Stand-in for the ClickHouse ``request_log`` insert. The gateway's
    SSE-error path appends the partial response here so the test can assert
    persistence happened before the error event was flushed."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    async def record(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


def _parse_sse(body: bytes) -> list[dict[str, Any] | str]:
    """Tiny SSE parser: returns the list of payloads in order. ``[DONE]`` is
    surfaced as the literal string ``"[DONE]"`` so the caller can assert on it."""
    out: list[dict[str, Any] | str] = []
    for line in body.decode().splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            out.append("[DONE]")
            continue
        out.append(json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_sse_emits_structured_error_event_then_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    settings = Settings(use_fake_provider=True)
    chaos = _ChaosStreamProvider(n_tokens=3)
    sink = _PersistenceStub()

    # Inject the persistence stub onto the streaming module so it lands the
    # partial-response row before flushing the error chunk.
    monkeypatch.setattr(streaming_module, "REQUEST_LOG_SINK", sink, raising=False)

    deps = Dependencies(
        settings=settings,
        router=Router(),
        providers={"fake": chaos, "real": FakeProvider()},
        cache=SemanticCache(redis=redis, embedder=HashEmbedder(), threshold=0.97),
        policies={
            "cheapest_first": CheapestFirst(),
            "latency_first": LatencyFirst(),
            "quality_first": QualityFirst(),
            "cost_capped": CostCapped(),
        },
    )
    app = create_app()
    app.state.deps = deps
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer pr_test_quality"},
            json={
                "model": "fake-large",
                "messages": [{"role": "user", "content": "stream me a story"}],
                "stream": True,
                "pulseroute_no_cache": True,
            },
        )
        body = resp.content

    events = _parse_sse(body)
    # Expect: 3 token chunks + 1 error chunk + [DONE]
    assert len(events) == 5, [type(e).__name__ for e in events]
    for i, ev in enumerate(events[:3]):
        assert isinstance(ev, dict)
        assert ev["choices"][0]["delta"]["content"] == f"tok{i}"
    err_event = events[3]
    assert isinstance(err_event, dict)
    assert "error" in err_event
    assert err_event["error"]["code"] == "upstream_disconnected"
    assert err_event["error"]["retryable"] is True
    # request_id should be propagated for client-side support tickets
    assert "request_id" in err_event["error"]
    assert events[4] == "[DONE]"

    # Persistence guarantee: the partial row was appended before [DONE] hit
    # the wire. The row carries error_code=upstream_disconnected so a real
    # ClickHouse writer can land it.
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["error_code"] == "upstream_disconnected"
    assert row["partial_completion"] == "tok0tok1tok2"
    assert row["tokens_out"] == 3

    await redis.flushall()
    await redis.aclose()
