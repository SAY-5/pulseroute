"""Property-based tests for the circuit-breaker state machine.

Hypothesis generates random sequences of (success|failure|wait) events; we
assert the resulting state transitions follow the documented rules and never
land in an invalid state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pulseroute_router.breaker import CircuitBreaker, CircuitState


class _Op(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    WAIT = "wait"


@dataclass(frozen=True)
class _Step:
    op: _Op
    dt: float  # seconds advanced before this op


_VALID_STATES = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}

_VALID_TRANSITIONS: set[tuple[CircuitState, CircuitState]] = {
    (CircuitState.CLOSED, CircuitState.CLOSED),
    (CircuitState.CLOSED, CircuitState.OPEN),
    (CircuitState.OPEN, CircuitState.OPEN),
    (CircuitState.OPEN, CircuitState.HALF_OPEN),
    (CircuitState.HALF_OPEN, CircuitState.CLOSED),
    (CircuitState.HALF_OPEN, CircuitState.OPEN),
    (CircuitState.HALF_OPEN, CircuitState.HALF_OPEN),
}


_step_strategy = st.builds(
    _Step,
    op=st.sampled_from(list(_Op)),
    dt=st.floats(min_value=0.0, max_value=120.0, allow_nan=False, allow_infinity=False),
)


@given(st.lists(_step_strategy, min_size=1, max_size=80))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_state_machine_only_walks_valid_transitions(steps: list[_Step]) -> None:
    b = CircuitBreaker(window_s=60, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30)
    now = 0.0
    prev_state = b.state
    for step in steps:
        now += step.dt
        # allow() may move OPEN -> HALF_OPEN once cooldown elapses.
        b.allow(now=now)
        assert b.state in _VALID_STATES
        assert (prev_state, b.state) in _VALID_TRANSITIONS, (prev_state, b.state)
        prev_state = b.state

        if step.op is _Op.SUCCESS:
            b.record_success(now=now)
        elif step.op is _Op.FAILURE:
            b.record_failure(now=now)
        # WAIT only advances the clock; the next loop iteration's allow() call
        # will pick up the elapsed time.
        assert b.state in _VALID_STATES
        assert (prev_state, b.state) in _VALID_TRANSITIONS, (prev_state, b.state)
        prev_state = b.state


@given(
    st.lists(
        st.sampled_from([_Op.SUCCESS, _Op.FAILURE]),
        min_size=20,
        max_size=80,
    )
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_opens_when_error_rate_crosses_threshold_on_failure(ops: list[_Op]) -> None:
    """The breaker only re-evaluates on ``record_failure`` (per the source).

    So we replay the sequence and assert that any time a failure is recorded
    while the rolling-window predicate is true (>= min_requests, error_rate
    >= threshold), the resulting state must be OPEN. After that, only further
    failures during HALF_OPEN can re-OPEN; successes during HALF_OPEN move
    back to CLOSED. We track the transitions inline."""
    b = CircuitBreaker(
        window_s=120, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    now = 0.0
    for op in ops:
        if op is _Op.SUCCESS:
            b.record_success(now=now)
        else:
            b.record_failure(now=now)
            n_events = len(b._events)
            n_errors = sum(1 for e in b._events if e.is_error)
            if n_events >= 20 and n_errors / n_events >= 0.5:
                # Either we were already OPEN, or this failure forced a
                # transition to OPEN. HALF_OPEN -> OPEN on failure is also OK.
                assert b.state == CircuitState.OPEN, (n_events, n_errors, b.state)
        now += 0.1


@given(
    n_failures=st.integers(min_value=20, max_value=40),
    cooldown_overshoot=st.floats(min_value=30.0, max_value=120.0, allow_nan=False),
)
@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_open_to_half_open_after_cooldown(n_failures: int, cooldown_overshoot: float) -> None:
    b = CircuitBreaker(
        window_s=600, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    for i in range(n_failures):
        b.record_failure(now=float(i) * 0.1)
    assert b.state == CircuitState.OPEN
    # allow() at any time before the cooldown elapses must NOT transition.
    assert not b.allow(now=float(n_failures) * 0.1 + 1.0)
    assert b.state == CircuitState.OPEN
    # After cooldown, a single allow() call probes and moves to HALF_OPEN.
    probe_time = float(n_failures) * 0.1 + cooldown_overshoot
    assert b.allow(now=probe_time)
    assert b.state == CircuitState.HALF_OPEN


@given(
    n_failures=st.integers(min_value=20, max_value=40),
    probe_succeeds=st.booleans(),
)
@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_half_open_resolves_to_closed_or_open(n_failures: int, probe_succeeds: bool) -> None:
    b = CircuitBreaker(
        window_s=600, min_requests=20, error_rate_threshold=0.5, half_open_after_s=30
    )
    for i in range(n_failures):
        b.record_failure(now=float(i) * 0.1)
    probe_time = float(n_failures) * 0.1 + 31.0
    b.allow(now=probe_time)
    assert b.state == CircuitState.HALF_OPEN
    if probe_succeeds:
        b.record_success(now=probe_time + 0.1)
        assert b.state == CircuitState.CLOSED
    else:
        b.record_failure(now=probe_time + 0.1)
        assert b.state == CircuitState.OPEN
