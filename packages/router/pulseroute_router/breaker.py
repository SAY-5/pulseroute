"""Per-(provider, model) circuit breaker.

State machine:
    CLOSED  --(error_rate >= threshold over window)-->  OPEN
    OPEN    --(half_open_after_s elapsed)-->            HALF_OPEN
    HALF_OPEN --(probe success)-->                      CLOSED
    HALF_OPEN --(probe failure)-->                      OPEN

The window is a rolling 1-minute bucket counted in events, evaluated each
record_failure / record_success call. We deliberately avoid background tasks
so the breaker is cheap and predictable in tests."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Event:
    ts: float
    is_error: bool


@dataclass
class CircuitBreaker:
    window_s: int = 60
    min_requests: int = 20
    error_rate_threshold: float = 0.5
    half_open_after_s: int = 30
    state: CircuitState = CircuitState.CLOSED
    _opened_at: float = 0.0
    _events: deque[_Event] = field(default_factory=deque)

    def _evict_stale(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0].ts < cutoff:
            self._events.popleft()

    def allow(self, now: float | None = None) -> bool:
        """Return True if a request should be sent through to the upstream."""
        now = now if now is not None else time.monotonic()
        if self.state == CircuitState.OPEN:
            if now - self._opened_at >= self.half_open_after_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._events.append(_Event(now, is_error=False))
        self._evict_stale(now)
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self._opened_at = 0.0

    def record_failure(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._events.append(_Event(now, is_error=True))
        self._evict_stale(now)
        if self.state == CircuitState.HALF_OPEN:
            self._open(now)
            return
        if len(self._events) >= self.min_requests:
            errors = sum(1 for e in self._events if e.is_error)
            if errors / len(self._events) >= self.error_rate_threshold:
                self._open(now)

    def _open(self, now: float) -> None:
        self.state = CircuitState.OPEN
        self._opened_at = now
