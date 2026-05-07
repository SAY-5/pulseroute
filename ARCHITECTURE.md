# PulseRoute — Architecture

This document covers the design choices that aren't obvious from the code.

## Routing policy taxonomy

Four policies ship in `packages/router/policies.py`. Each is a tiny dataclass
implementing the `RoutingPolicy` Protocol — a `rank(request, ctx)` method that
returns an ordered candidate list of `(provider, model)` tuples. The router
itself walks that list and short-circuits on the first candidate whose breaker
allows traffic.

| Policy | Decision rule | When to use |
|---|---|---|
| `CheapestFirst` | sort by `estimate_request_cost(request, model)` ascending | bulk batch jobs, exploration, dev keys |
| `LatencyFirst` | sort by `ctx.rolling_p95_ms[model]`, default 1.5s for unknowns | interactive UX, autocomplete |
| `QualityFirst` | sort by `ctx.quality_scores[model]` (eval-suite-derived), fallback to `MODEL_PRICES[model].quality_score` | high-stakes generation |
| `CostCapped` | `QualityFirst` until `spend_today >= 0.8 * ceiling`, then `CheapestFirst` | tenants with hard daily budgets |

Composition is by delegation, not inheritance. `CostCapped.rank` constructs a
`CheapestFirst` or `QualityFirst` and forwards the call. This keeps each policy
under 30 lines and makes the decision-table tests in
`tests/unit/test_routing_policies.py` exhaustive.

## Semantic cache design

### Two-stage lookup

1. **Exact-fingerprint check.** `prompt_fingerprint(messages)` produces a stable
   16-byte digest of the normalised conversation. Lookup is one Redis HGET.
2. **Cosine scan.** If the exact digest misses, embed the joined text and walk
   the tenant's vector hash, returning the best match if cosine >= threshold.

The scan is O(N) over the tenant's stored vectors. For demo workloads (≤ a few
thousand entries per tenant) this is fine. Production should swap RediSearch
HNSW behind the `Embedder` Protocol — the `SemanticCache` interface stays the
same.

### Normalisation rules

`packages/cache/normalize.py` applies in order:

1. NFKC unicode normalisation
2. lowercase
3. strip
4. collapse runs of whitespace to a single space

Anything beyond that — synonym expansion, stop-word removal, lemmatisation —
risks merging requests that genuinely differ. The current rules collide
`"Hello   world"` and `"hello world"` (good) but keep `"explain HTTP"` and
`"explain TCP"` distinct (also good). Tests in `tests/unit/test_cache.py` lock
these properties.

### Threshold reasoning

Default cosine threshold is **0.97**. The `HashEmbedder` is a deterministic
bag-of-token-hash projection — it gives high similarity for prompts that share
vocabulary but differ slightly in punctuation or wording. With a sentence-
transformers model in production, 0.92–0.95 is typical. The threshold is per
deployment via `PULSEROUTE_SEMANTIC_CACHE_THRESHOLD`.

### Per-tenant scoping

Cache keys are namespaced as `pulseroute:cache:{tenant}:entries|vectors`. There
is no cross-tenant lookup, by construction. Tests assert this.

## Circuit breaker math

Per `(provider, model)`, the breaker tracks events in a rolling 60-second
window. State transitions:

```
CLOSED  --(error_rate >= 0.5 over >= 20 events)-->  OPEN
OPEN    --(>= 30s elapsed)-->                        HALF_OPEN
HALF_OPEN --(probe success)-->                       CLOSED
HALF_OPEN --(probe failure)-->                       OPEN
```

Constants are configurable via `Settings.circuit_breaker_*`. The window is
event-counted, not wall-time-bucketed — this avoids background sweepers and
keeps the breaker O(1) per call.

### Two breaker backends

`Settings.breaker_backend` selects between:

- `in_process` (default) — one `CircuitBreaker` instance per `(provider, model)`
  per pod. Hermetic, dependency-free, what unit tests exercise.
- `redis` — `RedisBreakerClient` against the configured `redis_url`. Two Lua
  scripts under `packages/router/pulseroute_router/lua/`
  (`cb_record_and_check.lua`, `cb_allow.lua`) do the rolling-window event
  bookkeeping and the state transitions atomically server-side. One round
  trip per check.

Why two backends. With a single pod the in-process variant is sufficient and
trivially testable. When the gateway scales horizontally each pod sees only
its own slice of upstream traffic, so a faulty model can stay live longer
than necessary because no single pod exceeds the rolling-window threshold on
its own. The Redis backend pools events across pods so the predicate is
evaluated against the global error rate.

Redis keys (one set per `(provider, model)` pair):

| Key | Type | Contents |
|---|---|---|
| `cb:{provider}:{model}:events` | sorted set | one member per event, score = ts |
| `cb:{provider}:{model}:events:seq` | string | monotone counter for member disambiguation |
| `cb:{provider}:{model}:state` | string | `closed` \| `open` \| `half_open` |
| `cb:{provider}:{model}:opened_at` | string | ts when state moved to `open` |

Trade-offs. Redis becomes a SPOF for the breaker. The client falls back to
"allow" (degrade-CLOSED) on any Redis exception so a Redis outage does not
blackhole traffic; the caller is responsible for emitting a structured
`redis_breaker_degraded` event. For applications where false-negative-OPEN
is worse than false-positive-OPEN, set
`RedisBreakerClient.degrade_open_on_error=True` to flip the polarity.

Cross-pod agreement is verified by `tests/integration/test_distributed_breaker.py`,
which spins up two `RedisBreakerClient` instances against the same fakeredis
server and asserts that under 50 randomly interleaved (success|failure) event
sequences both clients observe the same state at every checkpoint.

## ClickHouse schema rationale

```sql
CREATE TABLE pulseroute.request_log (
  timestamp     DateTime64(3, 'UTC'),
  ...
  cost_usd      Float32,
  cache_hit     UInt8,
  error_code    LowCardinality(String) DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (tenant_id, timestamp);
```

- **`DateTime64(3)`** — millisecond precision matches realistic request timing
  without paying for nanoseconds. Storage is 8 bytes per value vs `DateTime`'s
  4, but the higher resolution is required for any meaningful latency analysis.
- **`PARTITION BY toYYYYMM(timestamp)`** — monthly partitions strike the
  sweet-spot between part-merge cost and partition-pruning effectiveness for
  the default 7–90 day retention. A daily partition would explode the part
  count for tenants with low traffic.
- **`ORDER BY (tenant_id, timestamp)`** — the most common admin query is "show
  me requests for tenant X in time range Y". This sort key turns it into a
  single-partition contiguous read.
- **`LowCardinality(String)`** for `route_reason`, `provider`, `model`,
  `error_code` — these have ≤ a few dozen distinct values. The dictionary
  encoding cuts disk by an order of magnitude and speeds up GROUP BY.
- **Hourly rollup MV** (`request_log_hourly`) — pre-aggregates the dashboard
  queries. Live queries at hour granularity hit the MV; live queries at
  request granularity hit the base table. The MV is a correct
  `AggregatingMergeTree` with `*State` aggregations so it composes with
  `*Merge` finalisers in the SELECT.

## Failover ladder timing

The user-facing path **does not retry inside the request**. The router picks a
primary; if the primary's breaker is OPEN the router walks to the next
candidate before the upstream call. If the upstream call itself fails, the
gateway returns:

```json
{ "error": { "code": "upstream_unavailable", "message": "...", "retryable": true } }
```

with HTTP 502, and the breaker counts the failure. The client decides whether
to retry. Rationale: gateway-side retries amplify outage blast-radius and
make P99 latency unpredictable; clients (especially batch workers) have
better context on whether retry is safe.

The async path (eval runs, canary scoring) DOES retry — Celery `autoretry_for`
with 5x exponential backoff and a DLQ for terminal failures. These jobs are
idempotent on `(suite_id, model, run_started_at)`.

## Streaming + partial failures

SSE streaming uses OpenAI's exact event shape so existing clients work
unchanged. If the upstream disconnects mid-stream the gateway emits one
synthetic chunk and a `[DONE]` terminator:

```
data: {"choices": [], "error": {"code": "upstream_disconnected", "retryable": true}}
data: [DONE]
```

This way clients that look for `[DONE]` continue to terminate cleanly while
clients that inspect chunks see the structured error.

## Drift detection — statistical power

The `golden_v1` suite has **N = 220 tasks** (200 GSM8K math, 5 code,
5 refusal, 10 RAG). Drift detection compares a candidate model's
per-category accuracy against the committed baseline and flags anything
that moves by more than the configured threshold (default 2 percentage
points).

For a binomial proportion `p` over `n` independent trials, the standard
error is

    SE(p) = sqrt(p * (1 - p) / n)

At the math sub-suite level (`n = 200`, `p ≈ 0.9` for a healthy model):

    SE = sqrt(0.9 * 0.1 / 200) ≈ 0.0212  →  ±2.1pp at 1σ

The 1.96σ minimum detectable effect (MDE) at p < 0.05 is therefore

    MDE_math = 1.96 * 0.0212 ≈ 0.042  →  ~4.2pp single-run

So a single run of the math sub-suite **cannot quite** clear the 2pp bar
on its own; it gets us within a factor of 2 of the goal. To reach 2pp at
p < 0.05 we use the canary window in addition to the math suite.

The Sprint-2 canary sampler defaults to a **1000-judgment rolling window**
(see `pulseroute canary run --alert-min-window`). Treating math (200) +
canary (800) as one combined window:

    SE_combined = sqrt(0.9 * 0.1 / 1000) ≈ 0.00949
    MDE_combined = 1.96 * 0.00949 ≈ 0.0186  →  ~1.9pp

That clears the 2pp threshold. Below 1000 the alert does not fire
(`should_alert` enforces this floor) so noise can't trip a regression
verdict.

To detect a 2pp regression on the math sub-suite alone at p < 0.05 we
would need either:

1. **More runs.** With `k` runs the SE shrinks by `sqrt(k)`:

       1.96 * sqrt(0.09 / (200 * k)) ≤ 0.02
       →  k ≥ 1.96² * 0.09 / (200 * 0.02²) ≈ 4.32

   so **5 deterministic runs** of the math sub-suite per candidate, or

2. **A bigger sample.** Inverting the same inequality for `n` at `k = 1`
   gives `n ≥ 1.96² * 0.09 / 0.02² ≈ 865` problems — covered by
   combining the 200-problem math suite with the 800-judgment canary
   floor.

Per-category power is worse for the small categories: the code and
refusal sub-suites (`n = 5` each) have `SE ≈ 0.13` at `p = 0.9`, so
single-run MDE is ~26pp. **Anything smaller than ~25pp on those small
categories needs multiple runs to debias.** RAG (`n = 10`) is at
`SE ≈ 0.095` → MDE ~19pp single-run. Math is the only category with
production-grade single-run power.

Math suite size grew 10 → 200 in Sprint 2; the combined math+canary
window of 1000 is what makes the README's 2pp claim actually defensible.
The committed baseline in `eval/baselines/golden_v1_fake.json` records
`n_tasks` so the math above is auditable, and
`tests/unit/test_eval_suite.py` enforces `len(MATH_SUITE) >= 200` so the
calculation can't silently drift.

## What's deliberately not here

- **Distributed circuit breaker.** In-process is enough for a single-pod
  deployment. A sketch of a Redis-backed sliding window lives in the roadmap.
- **Real-traffic canary scoring.** Wired (Sprint 2). The sampler in
  `services/eval-runner/pulseroute_eval/canary.py` pulls the most recent
  `cache_hit = 0 AND error_code = ''` rows out of `request_log`,
  deterministically subsamples them given
  `(window_start, window_end, sample_rate, seed)`, replays each through both
  arms, scores via the rubric at `eval/rubrics/canary_quality.md`, and
  persists to `canary_results`. Slack webhook is env-gated and off by
  default. See `pulseroute canary run --help`.
- **Per-request-token billing reconciliation.** `cost_usd` in `request_log` is
  estimated from the price table at request time. For invoicing-grade numbers
  pull provider usage exports nightly and reconcile.
- **Admin dashboard (Next.js).** Out of scope; REST + cursor pagination is
  enough for `curl` and the Python client.
- **GraphQL admin endpoint.** REST keeps the surface small.
- **K8s manifests / Helm chart.** The container builds; `docker-compose` runs
  the local stack. Production topology is intentionally left to the operator.
