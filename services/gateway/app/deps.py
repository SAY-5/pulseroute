"""Dependency container.

Built once at app start, threaded through routes via ``request.app.state.deps``.
Tests construct an alternate container with FakeProvider + fakeredis."""

from __future__ import annotations

from dataclasses import dataclass, field

import redis.asyncio as aioredis
from pulseroute_cache import HashEmbedder, SemanticCache
from pulseroute_router import (
    CheapestFirst,
    CostCapped,
    LatencyFirst,
    QualityFirst,
    Router,
    RoutingPolicy,
)
from pulseroute_router.provider import ChatProvider
from pulseroute_router.providers.anthropic import AnthropicProvider
from pulseroute_router.providers.fake import FakeProvider
from pulseroute_router.providers.openai import OpenAIProvider
from pulseroute_shared.settings import Settings, get_settings


@dataclass
class Dependencies:
    settings: Settings
    router: Router
    providers: dict[str, ChatProvider]
    cache: SemanticCache
    policies: dict[str, RoutingPolicy] = field(
        default_factory=lambda: {
            "cheapest_first": CheapestFirst(),
            "latency_first": LatencyFirst(),
            "quality_first": QualityFirst(),
            "cost_capped": CostCapped(),
        }
    )


def get_dependencies(settings: Settings | None = None) -> Dependencies:
    s = settings or get_settings()

    providers: dict[str, ChatProvider] = {"fake": FakeProvider()}
    if s.openai_api_key and not s.use_fake_provider:
        providers["openai"] = OpenAIProvider(api_key=s.openai_api_key)
    if s.anthropic_api_key and not s.use_fake_provider:
        providers["anthropic"] = AnthropicProvider(api_key=s.anthropic_api_key)

    redis = aioredis.from_url(s.redis_url, decode_responses=False)
    cache = SemanticCache(
        redis=redis, embedder=HashEmbedder(), threshold=s.semantic_cache_threshold
    )

    return Dependencies(
        settings=s,
        router=Router(),
        providers=providers,
        cache=cache,
    )
