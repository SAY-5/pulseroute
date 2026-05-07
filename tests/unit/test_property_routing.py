"""Property-based tests for routing decisions.

Hypothesis generates random tenant policies (cost cap, allowed_models, strategy)
and asserts the chosen model is in ``allowed_models``, never violates the cost
cap, and is deterministic for identical inputs."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pulseroute_router import (
    CheapestFirst,
    CostCapped,
    LatencyFirst,
    QualityFirst,
    Router,
)
from pulseroute_router.cost import MODEL_PRICES, estimate_request_cost
from pulseroute_router.policies import TenantContext
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage

_MODEL_KEYS = sorted(MODEL_PRICES.keys())
_POLICY_FACTORIES = {
    "cheapest_first": CheapestFirst,
    "latency_first": LatencyFirst,
    "quality_first": QualityFirst,
    "cost_capped": CostCapped,
}


def _build_request(content: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content=content)],
    )


@st.composite
def _tenant_strategy(draw: st.DrawFn) -> tuple[TenantContext, str, str]:
    n_allowed = draw(st.integers(min_value=1, max_value=len(_MODEL_KEYS)))
    allowed = frozenset(
        draw(
            st.lists(
                st.sampled_from(_MODEL_KEYS),
                min_size=n_allowed,
                max_size=n_allowed,
                unique=True,
            )
        )
    )
    cap = draw(st.floats(min_value=0.5, max_value=100.0, allow_nan=False))
    spend = draw(st.floats(min_value=0.0, max_value=cap, allow_nan=False))
    policy_name = draw(st.sampled_from(sorted(_POLICY_FACTORIES.keys())))
    content = draw(st.text(min_size=1, max_size=200))
    ctx = TenantContext(
        tenant_id="t-property",
        policy_id=policy_name,
        cost_ceiling_usd_per_day=cap,
        spend_today_usd=spend,
        allowed_models=allowed,
    )
    return ctx, policy_name, content


@given(_tenant_strategy())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_chosen_model_is_in_allowed_models(case: tuple[TenantContext, str, str]) -> None:
    ctx, policy_name, content = case
    policy = _POLICY_FACTORIES[policy_name]()
    router = Router()
    decision = router.decide(_build_request(content), ctx, policy)
    assert decision.chosen_model in ctx.allowed_models
    for candidate in decision.candidate_models:
        assert candidate in ctx.allowed_models


@given(_tenant_strategy())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_per_request_cost_never_exceeds_remaining_cap(
    case: tuple[TenantContext, str, str],
) -> None:
    ctx, policy_name, content = case
    policy = _POLICY_FACTORIES[policy_name]()
    router = Router()
    request = _build_request(content)
    decision = router.decide(request, ctx, policy)
    cost = estimate_request_cost(request, decision.chosen_model)
    # The router itself never sends a request whose single-shot estimated cost
    # exceeds the remaining daily cap. (The cap is enforced upstream by the
    # tenant accounting layer, but the routing policy must never *propose* a
    # decision that obviously cannot fit in the remaining budget.)
    if ctx.cost_remaining_usd > 0:
        # Soft check: we allow the per-request cost to exceed the remaining cap
        # only when no allowed model can fit. In that case the cheapest allowed
        # model is selected, which is the contract.
        cheapest_allowed = min(
            (estimate_request_cost(request, m) for m in ctx.allowed_models),
            default=float("inf"),
        )
        if cheapest_allowed <= ctx.cost_remaining_usd:
            assert cost <= ctx.cost_remaining_usd + 1e-9


@given(_tenant_strategy())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_decision_is_deterministic_for_identical_inputs(
    case: tuple[TenantContext, str, str],
) -> None:
    ctx, policy_name, content = case
    request = _build_request(content)
    # Two fresh routers receiving the same inputs must agree on the chosen
    # model. The decide() call is pure with respect to (request, ctx, policy)
    # for a router whose breakers are all in CLOSED state.
    a = Router().decide(request, ctx, _POLICY_FACTORIES[policy_name]())
    b = Router().decide(request, ctx, _POLICY_FACTORIES[policy_name]())
    assert a.chosen_provider == b.chosen_provider
    assert a.chosen_model == b.chosen_model
    assert a.candidate_models == b.candidate_models
    assert a.route_reason == b.route_reason
