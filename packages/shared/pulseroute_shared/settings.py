"""Process-wide settings loaded from env. Centralised so tests can override
without monkey-patching individual modules."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PULSEROUTE_", extra="ignore")

    postgres_dsn: str = "postgresql+asyncpg://pulseroute:pulseroute@localhost:5432/pulseroute"
    redis_url: str = "redis://localhost:6379/0"
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_db: str = "pulseroute"

    # Provider keys; absent in tests where respx mocks the wire.
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    semantic_cache_threshold: float = Field(0.97, ge=0.0, le=1.0)
    rate_limit_per_minute: int = 600
    request_timeout_s: float = 30.0

    circuit_breaker_window_s: int = 60
    circuit_breaker_min_requests: int = 20
    circuit_breaker_error_rate: float = 0.5
    circuit_breaker_half_open_after_s: int = 30
    # ``in_process`` keeps each pod's breaker local (legacy behaviour, hermetic
    # CI). ``redis`` shares state across pods via Lua scripts in
    # ``packages/router/pulseroute_router/lua``. The redis backend requires a
    # reachable Redis at ``redis_url``.
    breaker_backend: str = Field("in_process", pattern="^(in_process|redis)$")

    use_fake_provider: bool = False


def get_settings() -> Settings:
    return Settings()
