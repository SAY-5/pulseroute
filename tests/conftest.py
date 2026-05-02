"""Shared pytest fixtures.

These keep unit tests dependency-free: no Postgres, no real Redis, no real
provider keys."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pulseroute_cache import HashEmbedder, SemanticCache
from pulseroute_router import (
    CheapestFirst,
    CostCapped,
    LatencyFirst,
    QualityFirst,
    Router,
)
from pulseroute_router.providers.fake import FakeProvider


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield r
    await r.flushall()
    await r.aclose()


@pytest_asyncio.fixture
async def semantic_cache(fake_redis) -> SemanticCache:
    return SemanticCache(redis=fake_redis, embedder=HashEmbedder(), threshold=0.97)


@pytest_asyncio.fixture
async def app_client(fake_redis) -> AsyncIterator[AsyncClient]:
    """A FastAPI test client wired to FakeProvider + fakeredis."""
    from pulseroute_gateway.deps import Dependencies
    from pulseroute_gateway.main import create_app
    from pulseroute_shared.settings import Settings

    settings = Settings(use_fake_provider=True)
    deps = Dependencies(
        settings=settings,
        router=Router(),
        providers={"fake": FakeProvider()},
        cache=SemanticCache(redis=fake_redis, embedder=HashEmbedder(), threshold=0.97),
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
        yield client
