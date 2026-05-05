"""Bench harness for the PulseRoute gateway.

Replays a deterministic synthetic workload against the in-process gateway via
ASGI transport, then replays the same workload pinned to a single model for a
cost baseline. Emits a terse columnar text table to stdout and writes the full
record to ``bench/results/<timestamp>.json``.

What we measure
---------------
* Gateway-added latency (P50/P95/P99/P999/max) — ``request_total - upstream``.
  Cache hits count their full wall-clock since they bypass the provider.
* Cache hit rate — overall, on the duplicate subset, on the unique subset.
* Per-route_reason distribution — bucketed into ``cache_hit``,
  ``upstream_unavailable``, and the policy name (``quality_first``,
  ``cost_capped``, ``cheapest_first``, ``latency_first``).
* Cost: routed total vs the same workload pinned to ``fake-large`` (cache off).

Run
---
    python bench/bench.py                  # default 10k
    python bench/bench.py --requests 1000  # smoke
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Workload generation
# ---------------------------------------------------------------------------

# Three length buckets in characters. Using char counts keeps the generator
# trivially deterministic; the cost model uses ~4 chars/token internally.
SHORT_CHARS = (40, 200 * 4)  # < 200 tokens
MEDIUM_CHARS = (200 * 4, 2000 * 4)  # 200..2000 tokens
LONG_CHARS = (2000 * 4, 4000 * 4)  # > 2000 tokens

WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
    "bench",
    "route",
    "policy",
    "gateway",
    "tenant",
    "cache",
    "hit",
    "miss",
    "cost",
    "cap",
    "quality",
    "latency",
    "provider",
    "model",
    "deterministic",
    "synthetic",
]


@dataclass(frozen=True)
class Req:
    """A single bench request descriptor."""

    idx: int
    bucket: str  # "short" | "medium" | "long"
    is_duplicate: bool
    tenant_key: str  # bearer token
    prompt: str


def _make_prompt(rng: random.Random, low: int, high: int) -> str:
    n = rng.randint(low, high)
    out: list[str] = []
    total = 0
    while total < n:
        w = rng.choice(WORDS)
        out.append(w)
        total += len(w) + 1
    return " ".join(out)


def generate_workload(n: int, seed: int = 42) -> list[Req]:
    """Deterministic workload.

    Mix:
      * 70% short, 25% medium, 5% long
      * 30% are duplicates of an earlier unique request in the run
      * 5% are routed via tenant_costcap (the cost-cap flip is triggered
        by stepping ``spend_today`` over threshold partway through; see
        ``run_routed``)
    """
    rng = random.Random(seed)

    # First pass: assign bucket and tenant for every slot.
    slots: list[dict[str, Any]] = []
    for i in range(n):
        r = rng.random()
        if r < 0.70:
            bucket = "short"
        elif r < 0.95:
            bucket = "medium"
        else:
            bucket = "long"
        tenant_key = "pr_test_costcap" if rng.random() < 0.05 else "pr_test_quality"
        slots.append({"idx": i, "bucket": bucket, "tenant_key": tenant_key})

    # Mark 30% as duplicates. Duplicates copy a prior unique slot's prompt.
    dup_indices = set(rng.sample(range(1, n), k=int(n * 0.30)))

    # Second pass: assign prompts. Unique slots get a fresh prompt, duplicate
    # slots clone an earlier unique slot (same bucket if available, otherwise
    # any earlier unique).
    unique_pool: list[int] = []
    unique_by_bucket: dict[str, list[int]] = {"short": [], "medium": [], "long": []}
    requests: list[Req] = []

    for slot in slots:
        i = slot["idx"]
        bucket = slot["bucket"]
        if i in dup_indices and unique_pool:
            same_bucket = unique_by_bucket[bucket]
            source = rng.choice(same_bucket) if same_bucket else rng.choice(unique_pool)
            prompt = requests[source].prompt
            is_dup = True
        else:
            if bucket == "short":
                prompt = _make_prompt(rng, *SHORT_CHARS)
            elif bucket == "medium":
                prompt = _make_prompt(rng, *MEDIUM_CHARS)
            else:
                prompt = _make_prompt(rng, *LONG_CHARS)
            unique_pool.append(i)
            unique_by_bucket[bucket].append(i)
            is_dup = False
        requests.append(
            Req(
                idx=i,
                bucket=bucket,
                is_duplicate=is_dup,
                tenant_key=slot["tenant_key"],
                prompt=prompt,
            )
        )
    return requests


# ---------------------------------------------------------------------------
# Provider wrapper that records upstream wall-clock per call
# ---------------------------------------------------------------------------

_upstream_ms: contextvars.ContextVar[float] = contextvars.ContextVar("_upstream_ms", default=0.0)


class _TimedFakeProvider:
    """Wraps FakeProvider, records each call's elapsed ms into a contextvar.

    Using a contextvar means the request handler's task sees its own upstream
    elapsed even under concurrency (each asyncio Task has its own context)."""

    def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self.name = inner.name
        self.supported_models = inner.supported_models

    async def complete(self, request):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return await self._inner.complete(request)
        finally:
            _upstream_ms.set((time.perf_counter() - t0) * 1000.0)

    async def stream(self, request):  # type: ignore[no-untyped-def]
        async for chunk in self._inner.stream(request):
            yield chunk

    async def healthcheck(self) -> bool:
        return await self._inner.healthcheck()


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    idx: int
    bucket: str
    is_duplicate: bool
    tenant_key: str
    status: int
    total_ms: float
    upstream_ms: float
    gateway_added_ms: float
    cache_hit: bool
    route_reason: str
    cost_usd: float
    chosen_model: str | None


@dataclass
class Aggregate:
    n_requests: int
    n_ok: int
    latency: dict[str, float] = field(default_factory=dict)
    cache: dict[str, float] = field(default_factory=dict)
    route_reasons: dict[str, int] = field(default_factory=dict)
    errors_per_upstream: dict[str, int] = field(default_factory=dict)
    cost_routed_usd: float = 0.0
    cost_pinned_usd: float = 0.0
    savings_pct: float = 0.0


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def aggregate_samples(samples: list[Sample], cost_pinned_usd: float) -> Aggregate:
    n = len(samples)
    ok = sum(1 for s in samples if s.status == 200)
    added = sorted(s.gateway_added_ms for s in samples if s.status == 200)
    agg = Aggregate(n_requests=n, n_ok=ok)
    agg.latency = {
        "p50_ms": round(_percentile(added, 0.50), 4),
        "p95_ms": round(_percentile(added, 0.95), 4),
        "p99_ms": round(_percentile(added, 0.99), 4),
        "p999_ms": round(_percentile(added, 0.999), 4),
        "max_ms": round(max(added) if added else 0.0, 4),
        "mean_ms": round(statistics.fmean(added) if added else 0.0, 4),
    }

    dups = [s for s in samples if s.is_duplicate]
    uniq = [s for s in samples if not s.is_duplicate]
    hits = sum(1 for s in samples if s.cache_hit)
    hits_dup = sum(1 for s in dups if s.cache_hit)
    hits_uniq = sum(1 for s in uniq if s.cache_hit)
    agg.cache = {
        "hit_rate_overall": round(hits / n, 6) if n else 0.0,
        "hit_rate_on_dups": round(hits_dup / len(dups), 6) if dups else 0.0,
        "hit_rate_on_uniques": round(hits_uniq / len(uniq), 6) if uniq else 0.0,
        "n_dups": len(dups),
        "n_uniques": len(uniq),
    }

    for s in samples:
        agg.route_reasons[s.route_reason] = agg.route_reasons.get(s.route_reason, 0) + 1
        if s.status != 200:
            key = s.chosen_model or "unknown"
            agg.errors_per_upstream[key] = agg.errors_per_upstream.get(key, 0) + 1

    agg.cost_routed_usd = round(sum(s.cost_usd for s in samples), 6)
    agg.cost_pinned_usd = round(cost_pinned_usd, 6)
    if agg.cost_pinned_usd > 0:
        agg.savings_pct = round(
            100.0 * (agg.cost_pinned_usd - agg.cost_routed_usd) / agg.cost_pinned_usd, 4
        )
    return agg


# ---------------------------------------------------------------------------
# Routed run (all traffic through the gateway)
# ---------------------------------------------------------------------------


def _bucket_route_reason(raw: str | None, cache_hit: bool, status: int) -> str:
    if cache_hit:
        return "cache_hit"
    if status >= 500 or status == 0:
        return "upstream_unavailable"
    if not raw:
        return "unknown"
    # raw shape is "<policy>:<reason>"; collapse to the policy name.
    return raw.split(":", 1)[0]


async def run_routed(workload: list[Req]) -> tuple[list[Sample], dict[str, Any]]:
    """Run the workload through the gateway end-to-end."""
    import fakeredis.aioredis
    from httpx import ASGITransport, AsyncClient
    from pulseroute_cache import HashEmbedder, SemanticCache
    from pulseroute_gateway.deps import Dependencies
    from pulseroute_gateway.main import create_app
    from pulseroute_gateway.tenant import DEMO_TENANTS
    from pulseroute_router import (
        CheapestFirst,
        CostCapped,
        LatencyFirst,
        QualityFirst,
        Router,
    )
    from pulseroute_router.providers.fake import FakeProvider
    from pulseroute_shared.settings import Settings

    # Reset costcap tenant to a clean slate so repeated bench runs are stable,
    # and constrain both tenants to fake-only candidates so the cost number is
    # an apples-to-apples comparison against the fake-large baseline. (Without
    # this, ``quality_first`` ranks ``claude-3-5-sonnet`` first and we pay
    # premium-model rates while still calling FakeProvider — a misleading
    # number, not a useful one.)
    fake_only = frozenset({"fake-small", "fake-large"})
    DEMO_TENANTS["tenant_costcap"].spend_today_usd = 0.0
    DEMO_TENANTS["tenant_costcap"].allowed_models = fake_only
    DEMO_TENANTS["tenant_quality"].allowed_models = fake_only

    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    settings = Settings(use_fake_provider=True)
    fake = _TimedFakeProvider(FakeProvider())
    deps = Dependencies(
        settings=settings,
        router=Router(),
        providers={"fake": fake},
        cache=SemanticCache(redis=redis, embedder=HashEmbedder(), threshold=0.97),
        policies={
            "cheapest_first": CheapestFirst(),
            "latency_first": LatencyFirst(),
            "quality_first": QualityFirst(),
            "cost_capped": CostCapped(),
        },
    )
    app = create_app()
    app.state.deps = deps

    samples: list[Sample] = []
    # Trigger cost-cap flip 10% of the way through so the bulk of the run
    # exercises the post-flip (cost-capped -> cheapest) branch. Combined with
    # the ~5% costcap-tenant share, this lands ~5% of the workload on the
    # cost-cap-changed routing decision.
    flip_at = max(1, int(len(workload) * 0.10))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://bench") as client:
        for req in workload:
            if req.idx == flip_at:
                # Step spend over the 80% threshold (5.0 cap -> 4.5 spend).
                DEMO_TENANTS["tenant_costcap"].spend_today_usd = 4.5

            _upstream_ms.set(0.0)
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.tenant_key}"},
                    json={
                        "model": "fake-large",
                        "messages": [{"role": "user", "content": req.prompt}],
                    },
                )
                status = resp.status_code
                payload = resp.json() if resp.content else {}
            except Exception:
                status = 0
                payload = {}
            total_ms = (time.perf_counter() - t0) * 1000.0
            up_ms = _upstream_ms.get()
            pulseroute_meta = payload.get("pulseroute", {}) if isinstance(payload, dict) else {}
            cache_hit = bool(pulseroute_meta.get("cache_hit"))
            route_reason_raw = pulseroute_meta.get("route_reason")
            cost_usd = float(pulseroute_meta.get("cost_usd") or 0.0)
            chosen_model = payload.get("model") if isinstance(payload, dict) else None

            # Cache hits do no upstream work, so all wall-clock counts as
            # gateway-added. Non-200s record total minus whatever upstream
            # time we managed to capture (typically zero).
            gw_added = total_ms - up_ms if up_ms > 0 else total_ms
            samples.append(
                Sample(
                    idx=req.idx,
                    bucket=req.bucket,
                    is_duplicate=req.is_duplicate,
                    tenant_key=req.tenant_key,
                    status=status,
                    total_ms=total_ms,
                    upstream_ms=up_ms,
                    gateway_added_ms=max(0.0, gw_added),
                    cache_hit=cache_hit,
                    route_reason=_bucket_route_reason(route_reason_raw, cache_hit, status),
                    cost_usd=cost_usd,
                    chosen_model=chosen_model,
                )
            )

    meta = {
        "flip_at_request": flip_at,
        "concurrency": 1,
    }
    return samples, meta


# ---------------------------------------------------------------------------
# Pinned-baseline run (single-model, no cache, no routing)
# ---------------------------------------------------------------------------


async def run_pinned_baseline(workload: list[Req], pinned_model: str = "fake-large") -> float:
    """Replay the same workload directly against FakeProvider on a pinned model.

    Returns total cost in USD. We bypass the gateway and the cache deliberately
    — this is the 'no PulseRoute' counterfactual for cost comparison.
    """
    from pulseroute_router.cost import MODEL_PRICES
    from pulseroute_router.providers.fake import FakeProvider
    from pulseroute_shared.types import ChatCompletionRequest, ChatMessage

    provider = FakeProvider()
    price = MODEL_PRICES[pinned_model]
    total = 0.0
    for req in workload:
        body = ChatCompletionRequest(
            model=pinned_model,
            messages=[ChatMessage(role="user", content=req.prompt)],
        )
        resp = await provider.complete(body)
        total += (
            resp.prompt_tokens * price.input_per_1k / 1000.0
            + resp.completion_tokens * price.output_per_1k / 1000.0
        )
    return total


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_table(agg: Aggregate, meta: dict[str, Any]) -> str:
    lines: list[str] = []
    ts = meta["timestamp"]
    n = agg.n_requests
    lines.append(f"# pulseroute bench - {n} requests, mix=short70/med25/long5, dup=30%")
    lines.append(f"# {ts}, M-series Mac, fake-provider")
    lines.append("## gateway-added latency (ms, excludes upstream)")
    lines.append("p50    p95    p99    p999   max")
    lat = agg.latency
    lines.append(
        f"{lat['p50_ms']:<6} {lat['p95_ms']:<6} {lat['p99_ms']:<6} "
        f"{lat['p999_ms']:<6} {lat['max_ms']}"
    )
    lines.append("## cache")
    c = agg.cache
    lines.append(f"hit_rate_overall     : {c['hit_rate_overall'] * 100:.1f}%")
    lines.append(f"hit_rate_on_dups     : {c['hit_rate_on_dups'] * 100:.1f}%")
    lines.append(f"hit_rate_on_uniques  : {c['hit_rate_on_uniques'] * 100:.1f}%")
    lines.append("## routing decisions")
    lines.append(f"{'route_reason':<24} {'count':>6} {'pct':>7}")
    for reason, count in sorted(agg.route_reasons.items(), key=lambda kv: -kv[1]):
        pct = 100.0 * count / agg.n_requests if agg.n_requests else 0.0
        lines.append(f"{reason:<24} {count:>6} {pct:>6.1f}%")
    lines.append("## cost vs baseline")
    lines.append(f"total_cost_routed_usd  : {agg.cost_routed_usd:.6f}")
    lines.append(f"total_cost_pinned_usd  : {agg.cost_pinned_usd:.6f}")
    lines.append(f"savings_pct            : {agg.savings_pct:.1f}%")
    if agg.errors_per_upstream:
        lines.append("## errors per upstream")
        for upstream, count in sorted(agg.errors_per_upstream.items()):
            lines.append(f"{upstream:<24} {count}")
    lines.append("# Local-machine numbers on M-series Mac. See bench/README.md for methodology.")
    return "\n".join(lines)


def write_results(
    out_dir: Path,
    agg: Aggregate,
    samples: list[Sample],
    meta: dict[str, Any],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_safe = meta["timestamp"].replace(":", "").replace("-", "").replace(" ", "_")
    out = out_dir / f"{ts_safe}.json"
    payload = {
        "meta": meta,
        "summary": {
            "n_requests": agg.n_requests,
            "n_ok": agg.n_ok,
            "latency_ms": agg.latency,
            "cache": agg.cache,
            "route_reasons": agg.route_reasons,
            "errors_per_upstream": agg.errors_per_upstream,
            "cost": {
                "routed_usd": agg.cost_routed_usd,
                "pinned_usd": agg.cost_pinned_usd,
                "savings_pct": agg.savings_pct,
            },
        },
    }
    out.write_text(json.dumps(payload, indent=2))
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run_bench(n: int, seed: int, out_dir: Path) -> tuple[Aggregate, Path, str]:
    workload = generate_workload(n, seed=seed)
    samples, run_meta = await run_routed(workload)
    pinned_cost = await run_pinned_baseline(workload, pinned_model="fake-large")
    agg = aggregate_samples(samples, pinned_cost)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    meta = {
        "timestamp": ts,
        "n_requests": n,
        "seed": seed,
        "workload_mix": {"short": 0.70, "medium": 0.25, "long": 0.05},
        "duplicate_ratio": 0.30,
        "tenant_costcap_share": 0.05,
        "pinned_model": "fake-large",
        **run_meta,
    }
    table = render_table(agg, meta)
    path = write_results(out_dir, agg, samples, meta)
    return agg, path, table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PulseRoute gateway bench harness")
    parser.add_argument("--requests", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).parent / "results",
    )
    args = parser.parse_args(argv)

    _, path, table = asyncio.run(run_bench(args.requests, args.seed, args.out_dir))
    print(table)
    print(f"# results -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
