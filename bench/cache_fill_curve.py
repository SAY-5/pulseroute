"""Cache-fill curve harness.

Replays N sequential UNIQUE prompts through the gateway and reports the cache
hit rate per fixed-size window. Because every prompt is unique, the *expected*
hit rate is 0 across the run; the curve is meant to surface false-positive
collisions of the semantic cache fingerprint as the corpus grows. A spike
above zero in any window indicates either a normalisation collision or an
embedding collision at the configured threshold (0.97 cosine).

Output
------
Writes ``bench/results/cache_fill_curve.json`` with shape:
  {
    "meta": {"n_requests": N, "window_size": W, "threshold": 0.97, ...},
    "windows": [{"window": 0, "start": 0, "end": 999, "hits": 0, "hit_rate": 0.0}, ...],
    "summary": {"hits_total": 0, "hit_rate_overall": 0.0, "max_window_hit_rate": 0.0}
  }

Run
---
    python bench/cache_fill_curve.py                   # default 50000
    python bench/cache_fill_curve.py --requests 5000   # smoke
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

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


def _gen_unique_prompt(idx: int) -> str:
    """Deterministic, prompt-unique-per-idx generator. Each prompt embeds the
    decimal index so any collision implies a normalisation/embedding bug."""
    # Use a long-ish payload so the embedder has tokens to project; mostly
    # boilerplate but with idx interpolated in three places to avoid trivial
    # prefix overlap matching at the embedder level.
    return (
        f"unique-prompt-{idx} please summarise message number {idx} for tenant "
        f"with seed {idx} and report. context: filler tokens {idx % 97} "
        f"{(idx * 7) % 113} {(idx * 13) % 257}."
    )


async def run_cache_fill(
    n: int,
    window: int,
) -> dict[str, object]:
    fake_only = frozenset({"fake-small", "fake-large"})
    DEMO_TENANTS["tenant_quality"].allowed_models = fake_only

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
    windows: list[dict[str, float | int]] = []
    cur_start = 0
    cur_hits = 0
    total_hits = 0

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"authorization": "Bearer pr_test_quality"}
        for i in range(n):
            resp = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "fake-large",
                    "messages": [{"role": "user", "content": _gen_unique_prompt(i)}],
                },
            )
            payload = resp.json() if resp.content else {}
            meta = payload.get("pulseroute", {}) if isinstance(payload, dict) else {}
            if meta.get("cache_hit"):
                cur_hits += 1
                total_hits += 1
            if (i + 1) % window == 0:
                windows.append(
                    {
                        "window": len(windows),
                        "start": cur_start,
                        "end": i,
                        "hits": cur_hits,
                        "hit_rate": round(cur_hits / window, 6),
                    }
                )
                cur_start = i + 1
                cur_hits = 0
        # Trailing partial window
        if cur_start < n:
            tail = n - cur_start
            windows.append(
                {
                    "window": len(windows),
                    "start": cur_start,
                    "end": n - 1,
                    "hits": cur_hits,
                    "hit_rate": round(cur_hits / tail, 6),
                }
            )

    max_window_rate = max((w["hit_rate"] for w in windows), default=0.0)
    return {
        "meta": {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
            "n_requests": n,
            "window_size": window,
            "threshold": 0.97,
            "embedder": "HashEmbedder(dim=256)",
        },
        "windows": windows,
        "summary": {
            "hits_total": total_hits,
            "hit_rate_overall": round(total_hits / n, 6) if n else 0.0,
            "max_window_hit_rate": max_window_rate,
        },
    }


def render_ascii_chart(curve: dict[str, object], width: int = 60) -> str:
    """Tiny inline ASCII chart of hit-rate-per-window. The y-axis is per-window
    hit rate scaled to ``width`` columns; the x-axis is window index."""
    windows = curve["windows"]  # type: ignore[index]
    if not windows:
        return "(empty)"
    rates = [w["hit_rate"] for w in windows]  # type: ignore[index,union-attr]
    peak = max(rates) or 1.0
    lines: list[str] = []
    lines.append(
        f"# cache fill curve - {curve['meta']['n_requests']} unique prompts, "  # type: ignore[index]
        f"window={curve['meta']['window_size']}, peak_hit_rate={peak:.4f}"  # type: ignore[index]
    )
    lines.append("idx   range            rate    bar")
    for w in windows:
        bar_w = int(round((w["hit_rate"] / peak) * width)) if peak > 0 else 0  # type: ignore[index]
        bar = "#" * bar_w
        lines.append(
            f"{w['window']:<5} "  # type: ignore[index]
            f"{w['start']:>6}-{w['end']:<6}  "  # type: ignore[index]
            f"{w['hit_rate']:.4f}  {bar}"  # type: ignore[index]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PulseRoute cache-fill-curve harness")
    parser.add_argument("--requests", type=int, default=50_000)
    parser.add_argument("--window", type=int, default=1_000)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "results" / "cache_fill_curve.json",
    )
    args = parser.parse_args(argv)

    curve = asyncio.run(run_cache_fill(args.requests, args.window))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(curve, indent=2))
    print(render_ascii_chart(curve))
    print(f"# results -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
