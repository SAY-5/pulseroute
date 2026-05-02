"""Tenant context lookup. In tests we use the demo store; production wires this
to Postgres + a Redis spend counter."""

from __future__ import annotations

from pulseroute_router.policies import TenantContext

# Demo tenants — kept in code so the gateway boots cleanly without a Postgres
# round-trip for local dev. The seed script materialises the same shape into
# Postgres for integration tests.
DEMO_TENANTS: dict[str, TenantContext] = {
    "tenant_costcap": TenantContext(
        tenant_id="tenant_costcap",
        policy_id="cost_capped",
        cost_ceiling_usd_per_day=5.0,
        spend_today_usd=0.0,
        allowed_models=frozenset({"gpt-4o-mini", "claude-3-haiku", "fake-small", "fake-large"}),
    ),
    "tenant_quality": TenantContext(
        tenant_id="tenant_quality",
        policy_id="quality_first",
        cost_ceiling_usd_per_day=0.0,
        spend_today_usd=0.0,
        allowed_models=frozenset(
            {"gpt-4o", "claude-3-5-sonnet", "gpt-4o-mini", "fake-small", "fake-large"}
        ),
    ),
    "tenant_latency": TenantContext(
        tenant_id="tenant_latency",
        policy_id="latency_first",
        cost_ceiling_usd_per_day=0.0,
        spend_today_usd=0.0,
        allowed_models=frozenset({"gpt-4o-mini", "claude-3-haiku", "fake-small"}),
    ),
}


def get_tenant_context(tenant_id: str) -> TenantContext | None:
    return DEMO_TENANTS.get(tenant_id)
