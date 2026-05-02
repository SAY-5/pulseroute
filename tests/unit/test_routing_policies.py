"""Decision-table tests for the routing policy resolver."""

from __future__ import annotations

import pytest
from pulseroute_router import (
    CheapestFirst,
    CostCapped,
    LatencyFirst,
    QualityFirst,
    Router,
)
from pulseroute_router.policies import TenantContext
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage


def _req(model: str = "gpt-4o-mini") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hello world")],
    )


def _ctx(**overrides) -> TenantContext:
    base = dict(
        tenant_id="t",
        policy_id="quality_first",
        cost_ceiling_usd_per_day=10.0,
        spend_today_usd=0.0,
        allowed_models=frozenset({"gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet", "claude-3-haiku"}),
    )
    base.update(overrides)
    return TenantContext(**base)


def test_cheapest_first_picks_lowest_cost_first():
    ranked = CheapestFirst().rank(_req(), _ctx())
    # llama is excluded by allowed_models above, so cheapest in the allowed set is gpt-4o-mini.
    assert ranked[0][1] == "gpt-4o-mini"


def test_quality_first_picks_highest_quality_first():
    ranked = QualityFirst().rank(_req(), _ctx())
    assert ranked[0][1] == "claude-3-5-sonnet"


def test_latency_first_uses_rolling_p95():
    ctx = _ctx(rolling_p95_ms={"gpt-4o-mini": 80.0, "claude-3-haiku": 200.0, "gpt-4o": 600.0})
    ranked = LatencyFirst().rank(_req(), ctx)
    assert ranked[0][1] == "gpt-4o-mini"


def test_cost_capped_below_threshold_is_quality_first():
    ranked = CostCapped().rank(_req(), _ctx(spend_today_usd=1.0))
    assert ranked[0][1] == "claude-3-5-sonnet"


def test_cost_capped_above_threshold_flips_to_cheapest():
    ranked = CostCapped().rank(_req(), _ctx(spend_today_usd=9.5))
    assert ranked[0][1] == "gpt-4o-mini"


def test_cost_capped_with_no_ceiling_is_quality_first():
    ranked = CostCapped().rank(_req(), _ctx(cost_ceiling_usd_per_day=0.0))
    assert ranked[0][1] == "claude-3-5-sonnet"


def test_allowed_models_filter_drops_disallowed():
    ctx = _ctx(allowed_models=frozenset({"gpt-4o-mini"}))
    ranked = QualityFirst().rank(_req(), ctx)
    assert {m for _, m in ranked} == {"gpt-4o-mini"}


def test_no_candidates_raises():
    ctx = _ctx(allowed_models=frozenset({"nonexistent-model"}))
    with pytest.raises(ValueError):
        Router().decide(_req(), ctx, QualityFirst())


def test_router_decision_top_when_no_breakers_open():
    ctx = _ctx()
    decision = Router().decide(_req(), ctx, QualityFirst())
    assert decision.chosen_model == "claude-3-5-sonnet"
    assert decision.route_reason == "quality_first:primary"
    assert decision.policy_id == "quality_first"


def test_router_failover_when_top_breaker_open():
    router = Router()
    ctx = _ctx()
    decision = router.decide(_req(), ctx, QualityFirst())
    # Force the top candidate's breaker open by feeding many failures.
    breaker = router.breakers[(decision.chosen_provider, decision.chosen_model)]
    breaker.min_requests = 2
    breaker.error_rate_threshold = 0.5
    for _ in range(3):
        breaker.record_failure()
    new_decision = router.decide(_req(), ctx, QualityFirst())
    assert new_decision.chosen_model != decision.chosen_model
    assert new_decision.route_reason.endswith(":failover")


def test_router_returns_top_when_all_breakers_open():
    router = Router()
    ctx = _ctx(allowed_models=frozenset({"gpt-4o-mini"}))
    decision = router.decide(_req(), ctx, QualityFirst())
    breaker = router.breakers[(decision.chosen_provider, decision.chosen_model)]
    breaker.min_requests = 2
    for _ in range(3):
        breaker.record_failure()
    again = router.decide(_req(), ctx, QualityFirst())
    assert again.route_reason.endswith(":all_breakers_open")
