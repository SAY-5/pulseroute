"""Hermetic perf-smoke. Uses ASGITransport so it can run in CI without a
running gateway. Fails if P95 added latency exceeds the threshold."""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

from httpx import ASGITransport, AsyncClient


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=int(os.getenv("N", "200")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "16")))
    parser.add_argument(
        "--p95-budget-ms",
        type=float,
        default=float(os.getenv("P95_BUDGET_MS", "200")),
        help="Pass threshold; relaxed from 120ms in CI per README note.",
    )
    args = parser.parse_args()

    # Import lazily so we can pick up env-driven config.
    import fakeredis.aioredis
    from pulseroute_cache import HashEmbedder, SemanticCache
    from pulseroute_gateway.deps import Dependencies
    from pulseroute_gateway.main import create_app
    from pulseroute_router import (
        CheapestFirst,
        CostCapped,
        LatencyFirst,
        QualityFirst,
        Router,
    )
    from pulseroute_router.providers.fake import FakeProvider
    from pulseroute_shared.settings import Settings

    redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    settings = Settings(use_fake_provider=True)
    deps = Dependencies(
        settings=settings,
        router=Router(),
        providers={"fake": FakeProvider()},
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
    transport = ASGITransport(app=app)

    sem = asyncio.Semaphore(args.concurrency)
    results: list[tuple[int, float]] = []

    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def one(i: int) -> tuple[int, float]:
            async with sem:
                started = time.perf_counter()
                r = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer pr_test_quality"},
                    json={
                        "model": "fake-large",
                        "messages": [{"role": "user", "content": f"bench {i}"}],
                        "pulseroute_no_cache": True,
                    },
                )
                return r.status_code, (time.perf_counter() - started) * 1000

        results = await asyncio.gather(*(one(i) for i in range(args.n)))

    ok = sum(1 for s, _ in results if s == 200)
    lats = sorted(latency for _, latency in results)
    p50 = statistics.median(lats)
    p95 = lats[int(0.95 * (len(lats) - 1))]
    p99 = lats[int(0.99 * (len(lats) - 1))]
    print(f"requests={len(results)} ok={ok} p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")
    if ok != len(results):
        print(f"FAIL: only {ok}/{len(results)} succeeded", file=sys.stderr)
        return 1
    if p95 > args.p95_budget_ms:
        print(
            f"FAIL: p95 {p95:.1f}ms > budget {args.p95_budget_ms:.1f}ms",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
