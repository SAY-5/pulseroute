"""Two-pod distributed-breaker agreement tests.

Spins up two ``RedisBreakerClient`` instances against the same ``fakeredis``
server (the analogue of two gateway pods sharing one Redis) and asserts that
under randomised event interleavings both clients agree on the breaker
state at every checkpoint. ``fakeredis`` is hermetic, so this test runs in
the default CI unit job — the file lives in ``tests/integration`` only
because it covers the cross-component contract.

The Lua scripts mutate state atomically server-side, so the only ordering
that matters is the order in which the two clients submit ``record_*`` calls
to Redis. Hypothesis drives 50 random sequences and asserts the final state
agrees across both clients.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import fakeredis.aioredis
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pulseroute_router.breaker import CircuitState
from pulseroute_router.redis_breaker import RedisBreakerClient

PROVIDER = "fake"
MODEL = "fake-large"


@dataclass(frozen=True)
class _Step:
    pod: int  # 0 or 1
    is_error: bool
    dt: float


_step_strategy = st.builds(
    _Step,
    pod=st.integers(min_value=0, max_value=1),
    is_error=st.booleans(),
    dt=st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
)


async def _run_one_sequence(steps: list[_Step]) -> tuple[CircuitState, CircuitState, int]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    pod_a = RedisBreakerClient(
        redis=redis,
        window_s=120,
        min_requests=20,
        error_rate_threshold=0.5,
        half_open_after_s=30,
    )
    pod_b = RedisBreakerClient(
        redis=redis,
        window_s=120,
        min_requests=20,
        error_rate_threshold=0.5,
        half_open_after_s=30,
    )

    transitions = 0
    last_seen = CircuitState.CLOSED
    now = 1_000_000.0  # absolute seconds; both clients share this clock
    for step in steps:
        now += step.dt
        client = pod_a if step.pod == 0 else pod_b
        if step.is_error:
            new = await client.record_failure(PROVIDER, MODEL, now=now)
        else:
            new = await client.record_success(PROVIDER, MODEL, now=now)
        if new != last_seen:
            transitions += 1
            last_seen = new

        # Critical agreement check: after each event, both pods must read the
        # same state out of Redis. (The Lua script writes the state key
        # atomically; any disagreement here would indicate a broken script.)
        a_state = await pod_a.state(PROVIDER, MODEL)
        b_state = await pod_b.state(PROVIDER, MODEL)
        assert a_state == b_state, (a_state, b_state, step)

    final_a = await pod_a.state(PROVIDER, MODEL)
    final_b = await pod_b.state(PROVIDER, MODEL)
    await redis.flushall()
    await redis.aclose()
    return final_a, final_b, transitions


@pytest.mark.asyncio
@given(steps=st.lists(_step_strategy, min_size=10, max_size=80))
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
async def test_two_pods_agree_on_state(steps: list[_Step]) -> None:
    final_a, final_b, _ = await _run_one_sequence(steps)
    assert final_a == final_b


@pytest.mark.asyncio
async def test_open_threshold_crossed_visible_to_both_pods() -> None:
    """A scripted scenario: pod_a records 25 failures, pod_b reads OPEN.

    This is the canonical "different pods agree" assertion. With an
    in-process breaker this would fail because pod_b's local breaker would
    still be CLOSED."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    pod_a = RedisBreakerClient(redis=redis, min_requests=20, error_rate_threshold=0.5)
    pod_b = RedisBreakerClient(redis=redis, min_requests=20, error_rate_threshold=0.5)

    now = 1_000_000.0
    for i in range(25):
        await pod_a.record_failure(PROVIDER, MODEL, now=now + i * 0.1)

    assert await pod_a.state(PROVIDER, MODEL) == CircuitState.OPEN
    assert await pod_b.state(PROVIDER, MODEL) == CircuitState.OPEN
    # And critically, pod_b's allow() returns False even though pod_b
    # itself never recorded a failure.
    allowed = await pod_b.allow(PROVIDER, MODEL, now=now + 5.0)
    assert allowed is False

    await redis.flushall()
    await redis.aclose()


@pytest.mark.asyncio
async def test_open_to_half_open_to_closed_across_pods() -> None:
    """pod_a opens the breaker, pod_b probes after cooldown, pod_a sees closed."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    pod_a = RedisBreakerClient(
        redis=redis, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    pod_b = RedisBreakerClient(
        redis=redis, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )

    now = 1_000_000.0
    last_event_time = now
    for i in range(25):
        last_event_time = now + i * 0.1
        await pod_a.record_failure(PROVIDER, MODEL, now=last_event_time)
    assert await pod_a.state(PROVIDER, MODEL) == CircuitState.OPEN

    # 31s after the last event pod_b probes → HALF_OPEN
    probe_time = last_event_time + 31.0
    allowed = await pod_b.allow(PROVIDER, MODEL, now=probe_time)
    assert allowed is True
    assert await pod_a.state(PROVIDER, MODEL) == CircuitState.HALF_OPEN
    assert await pod_b.state(PROVIDER, MODEL) == CircuitState.HALF_OPEN

    # pod_b's probe succeeds → CLOSED
    new = await pod_b.record_success(PROVIDER, MODEL, now=probe_time + 0.1)
    assert new == CircuitState.CLOSED
    assert await pod_a.state(PROVIDER, MODEL) == CircuitState.CLOSED

    await redis.flushall()
    await redis.aclose()


@pytest.mark.asyncio
async def test_redis_outage_falls_back_to_closed() -> None:
    """If the Redis call raises, the client returns CLOSED (allow=True) so
    the gateway keeps serving traffic. The caller is responsible for logging
    the degradation."""

    class _BrokenRedis:
        async def eval(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

        async def get(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

        async def delete(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("redis is down")

    client = RedisBreakerClient(redis=_BrokenRedis())  # type: ignore[arg-type]
    # allow() returns True (degrade-CLOSED) by default.
    assert await client.allow(PROVIDER, MODEL, now=1_000_000.0) is True
    # state() falls back to CLOSED.
    assert await client.state(PROVIDER, MODEL) == CircuitState.CLOSED


def test_event_loop_compat() -> None:
    """Sanity: pytest-asyncio is in auto mode and the suite above resolves."""
    asyncio.get_event_loop_policy()
