"""Failover behaviour: a primary that throws should land on a fallback."""

from __future__ import annotations

import pytest
from pulseroute_router import Router
from pulseroute_router.policies import QualityFirst, TenantContext
from pulseroute_router.providers.fake import FakeProvider
from pulseroute_shared.types import (
    ChatCompletionRequest,
    ChatMessage,
    ProviderName,
)


@pytest.mark.asyncio
async def test_failover_walks_ladder_when_primary_breaker_open():
    """Drive the primary's breaker open via repeated failures, then assert the
    next candidate is chosen on the subsequent decision."""
    router = Router()
    ctx = TenantContext(
        tenant_id="t",
        policy_id="quality_first",
        cost_ceiling_usd_per_day=0.0,
        spend_today_usd=0.0,
        allowed_models=frozenset({"fake-large", "fake-small"}),
    )
    req = ChatCompletionRequest(
        model="fake-large", messages=[ChatMessage(role="user", content="hi")]
    )
    decision = router.decide(req, ctx, QualityFirst())
    assert decision.chosen_model == "fake-large"

    breaker = router.breakers[(decision.chosen_provider, decision.chosen_model)]
    breaker.min_requests = 2
    breaker.error_rate_threshold = 0.5
    for _ in range(3):
        breaker.record_failure()

    after = router.decide(req, ctx, QualityFirst())
    assert after.chosen_model == "fake-small"
    assert after.route_reason.endswith(":failover")
    assert after.chosen_provider == ProviderName.FAKE


@pytest.mark.asyncio
async def test_fake_provider_first_n_failures_then_success():
    p = FakeProvider(fail_n_times=2)
    req = ChatCompletionRequest(
        model="fake-large", messages=[ChatMessage(role="user", content="hi")]
    )
    with pytest.raises(RuntimeError):
        await p.complete(req)
    with pytest.raises(RuntimeError):
        await p.complete(req)
    out = await p.complete(req)
    assert out.content.startswith("fake[")
