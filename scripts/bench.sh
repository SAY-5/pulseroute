#!/usr/bin/env bash
# Local perf-smoke against the running gateway. Drives N concurrent clients
# against /v1/chat/completions with FakeProvider and prints P50/P95 added
# latency as observed by the client. Pure stdlib python — no k6 dependency.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
N="${N:-200}"
CONCURRENCY="${CONCURRENCY:-16}"
API_KEY="${API_KEY:-pr_test_quality}"

python3 - <<PY
import asyncio
import statistics
import time

import httpx


async def one(client, i):
    started = time.perf_counter()
    r = await client.post(
        "${BASE_URL}/v1/chat/completions",
        headers={"Authorization": "Bearer ${API_KEY}"},
        json={
            "model": "fake-large",
            "messages": [{"role": "user", "content": f"bench prompt {i}"}],
            "pulseroute_no_cache": True,
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    return r.status_code, elapsed_ms


async def main():
    sem = asyncio.Semaphore(${CONCURRENCY})
    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        async def guarded(i):
            async with sem:
                return await one(client, i)
        results = await asyncio.gather(*(guarded(i) for i in range(${N})))
    statuses = [s for s, _ in results]
    lats = sorted(latency for _, latency in results)
    ok = sum(1 for s in statuses if s == 200)
    p50 = statistics.median(lats)
    p95 = lats[int(0.95 * (len(lats) - 1))]
    p99 = lats[int(0.99 * (len(lats) - 1))]
    print(f"requests={len(results)} ok={ok} p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")
    if ok != len(results):
        raise SystemExit(1)
PY
