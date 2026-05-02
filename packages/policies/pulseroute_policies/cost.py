"""Per-tenant daily cost ceiling check."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostCheckResult:
    allowed: bool
    spend_usd: float
    ceiling_usd: float
    reason: str


@dataclass(frozen=True, slots=True)
class CostCeiling:
    """A simple ceiling check. Spend is supplied by the caller — this class
    intentionally does not own state, so it can be exercised in pure unit tests."""

    ceiling_usd_per_day: float
    soft_threshold_pct: float = 0.8

    def check(self, current_spend_usd: float, projected_request_cost: float) -> CostCheckResult:
        if self.ceiling_usd_per_day <= 0:
            return CostCheckResult(True, current_spend_usd, self.ceiling_usd_per_day, "no_ceiling")
        projected = current_spend_usd + projected_request_cost
        if projected > self.ceiling_usd_per_day:
            return CostCheckResult(
                False,
                current_spend_usd,
                self.ceiling_usd_per_day,
                "ceiling_exceeded",
            )
        if projected >= self.soft_threshold_pct * self.ceiling_usd_per_day:
            return CostCheckResult(
                True,
                current_spend_usd,
                self.ceiling_usd_per_day,
                "soft_threshold_reached",
            )
        return CostCheckResult(True, current_spend_usd, self.ceiling_usd_per_day, "within_budget")
