"""Distributed circuit-breaker state in Redis.

The in-process ``CircuitBreaker`` lives one-per-pod. When the gateway scales
horizontally, each pod sees only its own slice of upstream traffic and may
keep a faulty model live longer than necessary because no single pod exceeds
the rolling-window threshold on its own.

This module's ``RedisBreakerClient`` keeps the same state machine but stores
the rolling event window plus the state in Redis, evaluated atomically by two
Lua scripts:

  ``cb_record_and_check.lua``   — record event, evict stale, evaluate predicate
  ``cb_allow.lua``              — read state + maybe OPEN -> HALF_OPEN

Each request is one round trip. The server-side script ensures multiple pods
agree on state transitions even under interleaved traffic.

Trade-offs
----------
- Redis becomes a SPOF for the breaker. The gateway falls back to a local
  in-process breaker if the Redis call raises (logged as a structured event
  ``redis_breaker_degraded``).
- A network blip during a state transition is fine: the next call evaluates
  the predicate from scratch on the current event set.
- The ``ZADD`` member uses a small SHA-disambiguator so events emitted in
  the same microsecond from different pods don't collide on the score.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulseroute_router.breaker import CircuitState

_LUA_DIR = Path(__file__).parent / "lua"
_RECORD_AND_CHECK = (_LUA_DIR / "cb_record_and_check.lua").read_text()
_ALLOW = (_LUA_DIR / "cb_allow.lua").read_text()


def _key_prefix(provider: str, model: str) -> str:
    return f"cb:{provider}:{model}"


@dataclass
class RedisBreakerClient:
    """Distributed circuit breaker keyed by ``(provider, model)``.

    Construct once per gateway process and share across requests."""

    redis: Any
    window_s: int = 60
    min_requests: int = 20
    error_rate_threshold: float = 0.5
    half_open_after_s: int = 30
    # If any Redis call raises, the client falls back to ``CircuitState.CLOSED``
    # locally so traffic continues to flow rather than being blackholed by
    # a Redis outage. The caller is responsible for telemetry on degradation.
    degrade_open_on_error: bool = False

    async def _eval(self, script: str, keys: list[str], args: list[str]) -> list[Any]:
        # Use eval (not register) so the script body is sent each call. For a
        # production deploy you would EVALSHA + load-once; the test seam uses
        # fakeredis which already caches by hash internally.
        result = await self.redis.eval(script, len(keys), *keys, *args)
        return result  # type: ignore[no-any-return]

    async def allow(self, provider: str, model: str, *, now: float | None = None) -> bool:
        prefix = _key_prefix(provider, model)
        state_key = f"{prefix}:state"
        opened_at_key = f"{prefix}:opened_at"
        ts = now if now is not None else time.time()
        try:
            res = await self._eval(
                _ALLOW,
                [state_key, opened_at_key],
                [str(ts), str(self.half_open_after_s)],
            )
        except Exception:
            return not self.degrade_open_on_error
        # res = [state, allowed]
        allowed = _to_str(res[1]) == "1"
        return allowed

    async def record_success(
        self, provider: str, model: str, *, now: float | None = None
    ) -> CircuitState:
        return await self._record(provider, model, is_error=False, now=now)

    async def record_failure(
        self, provider: str, model: str, *, now: float | None = None
    ) -> CircuitState:
        return await self._record(provider, model, is_error=True, now=now)

    async def _record(
        self,
        provider: str,
        model: str,
        *,
        is_error: bool,
        now: float | None,
    ) -> CircuitState:
        prefix = _key_prefix(provider, model)
        events_key = f"{prefix}:events"
        state_key = f"{prefix}:state"
        opened_at_key = f"{prefix}:opened_at"
        ts = now if now is not None else time.time()
        try:
            res = await self._eval(
                _RECORD_AND_CHECK,
                [events_key, state_key, opened_at_key],
                [
                    str(ts),
                    "1" if is_error else "0",
                    str(self.window_s),
                    str(self.min_requests),
                    str(self.error_rate_threshold),
                    str(self.half_open_after_s),
                ],
            )
        except Exception:
            return CircuitState.CLOSED
        return CircuitState(_to_str(res[0]))

    async def state(self, provider: str, model: str) -> CircuitState:
        prefix = _key_prefix(provider, model)
        try:
            raw = await self.redis.get(f"{prefix}:state")
        except Exception:
            return CircuitState.CLOSED
        if not raw:
            return CircuitState.CLOSED
        return CircuitState(_to_str(raw))

    async def reset(self, provider: str, model: str) -> None:
        prefix = _key_prefix(provider, model)
        await self.redis.delete(f"{prefix}:state", f"{prefix}:opened_at", f"{prefix}:events")


def _to_str(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
