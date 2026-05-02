"""Circuit breaker state-machine tests."""

from __future__ import annotations

from pulseroute_router.breaker import CircuitBreaker, CircuitState


def test_starts_closed():
    b = CircuitBreaker()
    assert b.state == CircuitState.CLOSED
    assert b.allow()


def test_opens_after_threshold_errors():
    b = CircuitBreaker(window_s=60, min_requests=10, error_rate_threshold=0.5)
    for _ in range(10):
        b.record_failure(now=0.0)
    assert b.state == CircuitState.OPEN
    assert not b.allow(now=1.0)


def test_does_not_open_below_min_requests():
    b = CircuitBreaker(min_requests=10)
    for _ in range(5):
        b.record_failure(now=0.0)
    assert b.state == CircuitState.CLOSED


def test_half_open_after_cooldown():
    b = CircuitBreaker(min_requests=2, error_rate_threshold=0.5, half_open_after_s=30)
    b.record_failure(now=0.0)
    b.record_failure(now=0.0)
    assert b.state == CircuitState.OPEN
    assert b.allow(now=31.0)
    assert b.state == CircuitState.HALF_OPEN


def test_half_open_to_closed_on_probe_success():
    b = CircuitBreaker(min_requests=2, error_rate_threshold=0.5, half_open_after_s=30)
    b.record_failure(now=0.0)
    b.record_failure(now=0.0)
    b.allow(now=31.0)  # transition to half-open
    b.record_success(now=32.0)
    assert b.state == CircuitState.CLOSED


def test_half_open_to_open_on_probe_failure():
    b = CircuitBreaker(min_requests=2, error_rate_threshold=0.5, half_open_after_s=30)
    b.record_failure(now=0.0)
    b.record_failure(now=0.0)
    b.allow(now=31.0)
    b.record_failure(now=32.0)
    assert b.state == CircuitState.OPEN


def test_old_events_evicted_outside_window():
    b = CircuitBreaker(window_s=10, min_requests=5, error_rate_threshold=0.5)
    for _ in range(5):
        b.record_failure(now=0.0)
    # Many seconds later, success-only traffic should not keep the circuit open.
    assert b.state == CircuitState.OPEN
    # New successes outside the window evict the old failures.
    for i in range(10):
        b.record_success(now=100.0 + i)
    # After the cooldown the breaker would have transitioned anyway, but the
    # rolling window evicts old failures from the rate calc.
    assert all(not e.is_error for e in b._events)
