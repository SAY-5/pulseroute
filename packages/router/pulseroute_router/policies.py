"""Routing policy taxonomy.

A policy answers a single question: given a request and a tenant context, which
ordered list of (provider, model) candidates should we try? The Router walks
that ladder, honouring circuit breakers and a strict total-time budget."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pulseroute_shared.types import ChatCompletionRequest, ProviderName, RouteDecision

from pulseroute_router.breaker import CircuitBreaker
from pulseroute_router.cost import MODEL_PRICES, estimate_request_cost


@dataclass
class TenantContext:
    tenant_id: str
    policy_id: str
    cost_ceiling_usd_per_day: float
    spend_today_usd: float
    allowed_models: frozenset[str]
    pii_redaction: bool = False
    rolling_p95_ms: dict[str, float] = field(default_factory=dict)
    quality_scores: dict[str, float] = field(default_factory=dict)

    @property
    def cost_remaining_usd(self) -> float:
        return max(0.0, self.cost_ceiling_usd_per_day - self.spend_today_usd)


class RoutingPolicy(Protocol):
    """Protocol for a routing policy."""

    name: str

    def rank(
        self, request: ChatCompletionRequest, ctx: TenantContext
    ) -> list[tuple[ProviderName, str]]: ...


def _filter_allowed(
    models: list[tuple[ProviderName, str]], ctx: TenantContext
) -> list[tuple[ProviderName, str]]:
    if not ctx.allowed_models:
        return models
    return [(p, m) for p, m in models if m in ctx.allowed_models]


@dataclass
class CheapestFirst:
    """Sort all allowed candidates by predicted cost, ascending."""

    name: str = "cheapest_first"

    def rank(
        self, request: ChatCompletionRequest, ctx: TenantContext
    ) -> list[tuple[ProviderName, str]]:
        candidates = [(p.provider, p.model) for p in MODEL_PRICES.values()]
        candidates = _filter_allowed(candidates, ctx)
        candidates.sort(key=lambda pm: estimate_request_cost(request, pm[1]))
        return candidates


@dataclass
class LatencyFirst:
    """Sort by rolling P95 latency, falling back to a default if unknown."""

    name: str = "latency_first"
    default_p95_ms: float = 1500.0

    def rank(
        self, request: ChatCompletionRequest, ctx: TenantContext
    ) -> list[tuple[ProviderName, str]]:
        candidates = [(p.provider, p.model) for p in MODEL_PRICES.values()]
        candidates = _filter_allowed(candidates, ctx)
        candidates.sort(key=lambda pm: ctx.rolling_p95_ms.get(pm[1], self.default_p95_ms))
        return candidates


@dataclass
class QualityFirst:
    """Sort by latest eval-suite quality score, falling back to the price-table score."""

    name: str = "quality_first"

    def rank(
        self, request: ChatCompletionRequest, ctx: TenantContext
    ) -> list[tuple[ProviderName, str]]:
        candidates = [(p.provider, p.model) for p in MODEL_PRICES.values()]
        candidates = _filter_allowed(candidates, ctx)
        candidates.sort(
            key=lambda pm: ctx.quality_scores.get(pm[1], MODEL_PRICES[pm[1]].quality_score),
            reverse=True,
        )
        return candidates


@dataclass
class CostCapped:
    """QualityFirst until the tenant has burned 80% of its daily cap; then CheapestFirst."""

    name: str = "cost_capped"
    threshold_pct: float = 0.8

    def rank(
        self, request: ChatCompletionRequest, ctx: TenantContext
    ) -> list[tuple[ProviderName, str]]:
        if ctx.cost_ceiling_usd_per_day <= 0:
            base: RoutingPolicy = QualityFirst()
        else:
            burn = ctx.spend_today_usd / ctx.cost_ceiling_usd_per_day
            base = CheapestFirst() if burn >= self.threshold_pct else QualityFirst()
        return base.rank(request, ctx)


POLICIES: dict[str, RoutingPolicy] = {
    "cheapest_first": CheapestFirst(),
    "latency_first": LatencyFirst(),
    "quality_first": QualityFirst(),
    "cost_capped": CostCapped(),
}


@dataclass
class Router:
    """Resolves a routing decision and walks the failover ladder.

    The router itself does not call providers — it is a pure decision engine.
    The gateway request handler owns the actual upstream call, so this layer
    stays trivially testable."""

    breakers: dict[tuple[ProviderName, str], CircuitBreaker] = field(default_factory=dict)

    def _breaker_for(self, provider: ProviderName, model: str) -> CircuitBreaker:
        key = (provider, model)
        if key not in self.breakers:
            self.breakers[key] = CircuitBreaker()
        return self.breakers[key]

    def decide(
        self,
        request: ChatCompletionRequest,
        ctx: TenantContext,
        policy: RoutingPolicy,
    ) -> RouteDecision:
        """Choose a (provider, model) for this request.

        Returns the first candidate whose breaker allows traffic. If none do, we
        still return the top-ranked candidate so the caller can produce a
        consistent ``upstream_unavailable`` error response — never silently fall
        back without telemetry."""
        ranked = policy.rank(request, ctx)
        if not ranked:
            raise ValueError("no candidate models available for tenant policy")

        chosen: tuple[ProviderName, str] | None = None
        reason = "primary"
        for i, (provider, model) in enumerate(ranked):
            if self._breaker_for(provider, model).allow():
                chosen = (provider, model)
                if i > 0:
                    reason = "failover"
                break

        if chosen is None:
            chosen = ranked[0]
            reason = "all_breakers_open"

        return RouteDecision(
            chosen_provider=chosen[0],
            chosen_model=chosen[1],
            candidate_models=[m for _, m in ranked],
            route_reason=f"{policy.name}:{reason}",
            policy_id=ctx.policy_id,
            cost_cap_remaining_usd=ctx.cost_remaining_usd,
        )
