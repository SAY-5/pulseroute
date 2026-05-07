"""Unit tests for ``RedisBreakerClient`` against ``fakeredis[lua]``.

These exercise the Lua scripts end-to-end inside a single client. Two-pod
agreement properties live in ``tests/integration/test_distributed_breaker.py``.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from pulseroute_router.breaker import CircuitState
from pulseroute_router.redis_breaker import RedisBreakerClient

PROVIDER = "fake"
MODEL = "fake-large"


@pytest.mark.asyncio
async def test_starts_closed_with_empty_state() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(redis=redis)
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED
    assert await client.allow(PROVIDER, MODEL, now=1.0) is True
    await redis.aclose()


@pytest.mark.asyncio
async def test_open_after_threshold_failures() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(redis=redis, min_requests=20, error_rate_threshold=0.5)
    for i in range(25):
        await client.record_failure(PROVIDER, MODEL, now=1_000_000.0 + i * 0.1)
    assert await client.state(PROVIDER, MODEL) == CircuitState.OPEN
    # allow() inside cooldown returns False
    assert await client.allow(PROVIDER, MODEL, now=1_000_000.0 + 5.0) is False
    await redis.aclose()


@pytest.mark.asyncio
async def test_does_not_open_below_min_requests() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(redis=redis, min_requests=20, error_rate_threshold=0.5)
    for i in range(10):
        await client.record_failure(PROVIDER, MODEL, now=1_000_000.0 + i * 0.1)
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED
    await redis.aclose()


@pytest.mark.asyncio
async def test_open_to_half_open_after_cooldown() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(
        redis=redis, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    last = 1_000_000.0
    for i in range(25):
        last = 1_000_000.0 + i * 0.1
        await client.record_failure(PROVIDER, MODEL, now=last)
    assert await client.state(PROVIDER, MODEL) == CircuitState.OPEN
    allowed = await client.allow(PROVIDER, MODEL, now=last + 31.0)
    assert allowed is True
    assert await client.state(PROVIDER, MODEL) == CircuitState.HALF_OPEN
    await redis.aclose()


@pytest.mark.asyncio
async def test_half_open_to_closed_on_probe_success() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(
        redis=redis, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    last = 1_000_000.0
    for i in range(25):
        last = 1_000_000.0 + i * 0.1
        await client.record_failure(PROVIDER, MODEL, now=last)
    await client.allow(PROVIDER, MODEL, now=last + 31.0)
    assert await client.state(PROVIDER, MODEL) == CircuitState.HALF_OPEN
    new = await client.record_success(PROVIDER, MODEL, now=last + 31.1)
    assert new == CircuitState.CLOSED
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED
    await redis.aclose()


@pytest.mark.asyncio
async def test_half_open_to_open_on_probe_failure() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(
        redis=redis, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    last = 1_000_000.0
    for i in range(25):
        last = 1_000_000.0 + i * 0.1
        await client.record_failure(PROVIDER, MODEL, now=last)
    await client.allow(PROVIDER, MODEL, now=last + 31.0)
    assert await client.state(PROVIDER, MODEL) == CircuitState.HALF_OPEN
    new = await client.record_failure(PROVIDER, MODEL, now=last + 31.1)
    assert new == CircuitState.OPEN
    await redis.aclose()


@pytest.mark.asyncio
async def test_reset_clears_keys() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    client = RedisBreakerClient(redis=redis)
    for i in range(5):
        await client.record_failure(PROVIDER, MODEL, now=1.0 + i)
    await client.reset(PROVIDER, MODEL)
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED
    await redis.aclose()


@pytest.mark.asyncio
async def test_redis_outage_falls_back_to_allow() -> None:
    """When the Redis call raises, the client returns CLOSED + allow=True
    by default so traffic continues to flow."""

    class _BrokenRedis:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

        async def get(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

        async def delete(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

    client = RedisBreakerClient(redis=_BrokenRedis())  # type: ignore[arg-type]
    assert await client.allow(PROVIDER, MODEL, now=1.0) is True
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED
    # record_* also degrade safely
    new = await client.record_failure(PROVIDER, MODEL, now=1.0)
    assert new == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_redis_outage_with_degrade_open_returns_false() -> None:
    class _BrokenRedis:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

    client = RedisBreakerClient(redis=_BrokenRedis(), degrade_open_on_error=True)  # type: ignore[arg-type]
    assert await client.allow(PROVIDER, MODEL, now=1.0) is False
