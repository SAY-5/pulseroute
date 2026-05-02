"""API key resolution.

Production wires this to Postgres. For tests and local dev we fall back to a
small in-memory map keyed off the same hashing scheme so the surface stays
consistent."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    tenant_id: str
    policy_id: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


_DEMO_KEYS: dict[str, ResolvedKey] = {
    hash_key("pr_test_costcap"): ResolvedKey("tenant_costcap", "cost_capped"),
    hash_key("pr_test_quality"): ResolvedKey("tenant_quality", "quality_first"),
    hash_key("pr_test_latency"): ResolvedKey("tenant_latency", "latency_first"),
}


def resolve_api_key(authorization_header: str | None) -> ResolvedKey | None:
    if not authorization_header:
        return None
    token = authorization_header.removeprefix("Bearer ").strip()
    if not token:
        return None
    return _DEMO_KEYS.get(hash_key(token))
