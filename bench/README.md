# PulseRoute bench harness

`bench/bench.py` is a separate program from the test suite. It replays a
deterministic synthetic workload through the in-process gateway (ASGI
transport, FakeProvider, fakeredis) and writes a JSON artifact to
`bench/results/<timestamp>.json`.

```bash
make bench                  # default 10k requests
make bench REQUESTS=1000    # smoke
python bench/bench.py --requests 5000 --seed 42
```

## What is measured

| metric | definition |
|---|---|
| Gateway-added latency | `total_request_wall_clock - upstream_provider_wall_clock` per request, aggregated to P50/P95/P99/P999/max. Cache hits count their full wall-clock since they bypass the provider. |
| Cache hit rate | Overall, plus broken out for the duplicate subset and the unique-prompt subset. |
| Routing decisions | Count by `route_reason`, bucketed into `cache_hit`, `upstream_unavailable`, and the policy name (`quality_first`, `cost_capped`, etc). |
| Cost — routed | Sum of per-request `cost_usd` returned in the gateway response (`pulseroute.cost_usd`). |
| Cost — pinned | Same workload replayed directly against `FakeProvider` on the pinned model (`fake-large`), with no gateway and no cache. |
| Errors per upstream | Count of non-200 responses keyed by chosen model. |

## What is held constant

- Workload is generated from `random.Random(42)`. Re-running the bench with
  the same `--seed` produces a byte-identical sequence of requests.
- The same workload is replayed against both the routed run and the pinned
  baseline.
- `FakeProvider` is deterministic: identical input -> identical output and
  identical token counts.
- Both `tenant_quality` and `tenant_costcap` are constrained to
  `{fake-small, fake-large}` for the duration of the run so the cost number
  is an apples-to-apples comparison against the fake-large baseline. Without
  this constraint, `quality_first` would rank premium models (gpt-4o,
  claude-3-5-sonnet) first and the bench would charge premium-model rates
  while still calling `FakeProvider` — a misleading number, not a useful one.

## What varies

- **Length mix.** 70% short prompts (< 200 tokens), 25% medium (200-2000),
  5% long (> 2000).
- **Duplication.** 30% of slots are duplicates of an earlier unique slot in
  the same length bucket. This is what exercises the semantic cache.
- **Cost-cap flip.** ~5% of traffic uses `tenant_costcap` (policy
  `cost_capped`). At 10% of the way through the run we step
  `tenant_costcap.spend_today_usd` over the 80% threshold, which flips the
  policy from `quality_first` to `cheapest_first` for the remainder. That's
  the "5% trigger a routing decision change" lever.

## What the numbers mean

- **Gateway-added latency is the wrap, NOT the upstream time.** It is
  `total_request_time - upstream_provider_time`. With FakeProvider that
  upstream time is microseconds, so what we report is dominated by the
  in-process work the gateway actually does: API-key resolve, tenant
  lookup, cache scan, routing decision, response shaping.
- **Cache scan dominates at large unique-prompt counts.** The semantic
  cache backing `bench/bench.py` is the in-process `SemanticCache` over
  fakeredis, which is O(N) in stored vectors per lookup (it does a full
  HGETALL + cosine scan per request). At 10k requests with ~7k unique
  prompts, the cache scan is the largest single contributor to the
  reported gateway-added latency, and the latency grows over the run as
  the corpus grows. **Production deployments swap the in-process scan for
  RediSearch HNSW** (see `packages/cache/pulseroute_cache/semantic.py`
  module docstring); steady-state P95 there is bounded by HNSW lookup
  time, not corpus size. The hermetic
  `scripts/bench_asgi.py --pulseroute_no_cache true` bench reports the
  cache-free wrap latency separately at ~5 ms P95.
- **Upstream provider latency dominates end-to-end on production
  traffic.** This bench excludes it deliberately; the
  `provider_upstream_latency_seconds` Prometheus histogram in
  `services/gateway/app/routes/chat.py` reports the upstream side.
- **Concurrency = 1.** The bench runs requests sequentially through the
  ASGI transport. This isolates the wrap latency from scheduler jitter.
  See `scripts/bench_asgi.py` for a concurrent variant.
- **Cost-savings number is workload-dependent.** The 30% duplicate ratio is
  realistic for some workloads (autocomplete, repeated translations,
  tool-calling templates, FAQ deflection) but **not** for free-form chat,
  which would see ~0% cache benefit. Treat the savings figure as an upper
  bound for cache-friendly workloads, not a forecast for general chat.
- **Cost is estimated, not billed.** The per-request `cost_usd` comes from
  the `MODEL_PRICES` table in `packages/router/pulseroute_router/cost.py`.
  Real billing reconciles against provider invoices.
- **The pinned baseline calls `FakeProvider` directly with no cache.** It is
  the "no PulseRoute" counterfactual: every request pays full price. The
  routed number wins on (a) cache hits skip the cost entirely, (b)
  cost-capped traffic flips to `fake-small` after the spend threshold.

## What the numbers do NOT mean

- **Not a production SLO.** This is a hermetic bench on a local machine;
  add 1-2 orders of magnitude for real network + provider RTT.
- **Not a moderation or quality benchmark.** See `eval/baselines/` and
  `make bench-eval` for accuracy on the golden suite.
- **Not a cost forecast.** Real workloads vary in duplication ratio,
  prompt length distribution, and tenant policy mix. Re-run the bench
  against your own workload sample before quoting the savings number.

## Output

stdout: a terse columnar text table (see `bench.py:render_table`), with the
same shape as the example in `pulseroute bench --help`.

JSON: `bench/results/<ISO8601>.json` with keys `meta`, `summary.latency_ms`,
`summary.cache`, `summary.route_reasons`, `summary.errors_per_upstream`,
`summary.cost`. The README's "Performance" section quotes from the latest
artifact in this directory.

## Scaling limit

`bench.py` runs the full gateway in-process over `fakeredis`. The semantic
cache lookup is **O(N)** in stored vectors per request (per the docstring in
`packages/cache/pulseroute_cache/semantic.py`): a full `HGETALL` plus a
Python cosine scan. With 30 % duplicate ratio the corpus grows linearly with
the request count, so the bench's wall-clock cost is roughly **O(N²/2)**.

Practical ceiling on a single core: **~10–15k requests** before the run is
slower than is useful (a 30k run on an M-series Mac does not finish in a
half-hour). To exercise larger scales, swap the in-process scan for the
RediSearch HNSW path documented in `packages/cache/pulseroute_cache/semantic.py` —
the harness API does not change.

## Cache-fill curve

`bench/cache_fill_curve.py` is a separate harness focused on the **false
positive** axis. It replays N sequential **unique** prompts through the
gateway and records the per-window hit rate. Because every prompt is
unique by construction, the expected hit rate is 0 % across every window.
A non-zero spike means the prompt-fingerprint normaliser collided two
different prompts onto the same key, or the embedder produced a
near-identical vector for two semantically distinct prompts at the
configured 0.97 cosine threshold.

```bash
make bench-cache-fill                                    # default 50000
python bench/cache_fill_curve.py --requests 2000 --window 200
```

Output is `bench/results/cache_fill_curve.json` plus a tiny ASCII chart on
stdout. The same scaling caveat applies — the cache fill harness inherits
the O(N) lookup, so production validation should run with HNSW or against
a smaller `--requests` budget.

## Bench regression gate

`bench/regress.py` compares two bench-result JSONs and exits non-zero when
any tracked metric drifts beyond a threshold (default 30 %).

```bash
make bench-regress \
  BENCH_BASELINE=bench/results/baseline_1k.json \
  BENCH_FRESH=bench/results/<latest>.json
```

Tracked metrics:

| metric | direction | reason |
|---|---|---|
| `latency_ms.p50_ms` | higher_is_worse | catastrophic latency regressions |
| `latency_ms.p95_ms` | higher_is_worse | tail-latency regressions |
| `latency_ms.p99_ms` | higher_is_worse | tail-latency regressions |
| `cache.hit_rate_overall` | lower_is_worse | cache stops hitting at all |
| `cache.hit_rate_on_dups` | lower_is_worse | duplicate path stopped working |
| `cost.savings_pct` | lower_is_worse | routing/cache stopped saving cost |

CI runs a 1000-request fresh bench against `bench/results/baseline_1k.json`
on every push (job `bench-regress`). The 30 % threshold is wide on
purpose: GitHub Actions runners have noisy latency tails. A genuine
regression (cache hit rate collapsed, P95 doubled) clears the threshold
immediately.
