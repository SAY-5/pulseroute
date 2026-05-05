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

The breaker deliberately stays in-process. A distributed breaker (e.g. Redis-
backed sliding window) is on the roadmap; for a single-pod deployment the
in-process variant is sufficient and far simpler to reason about. When the
gateway is horizontally scaled, each pod's breaker independently observes its
own traffic — which is actually desirable: a pod whose connection to a
provider is degraded should open *its* breaker without affecting healthy pods.

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

The `golden_v1` suite has **N = 30 tasks** (10 math, 5 code, 5 refusal,
10 RAG). Drift detection compares a candidate model's per-category
accuracy against the committed baseline and flags anything that moves by
more than the configured threshold (default 2 percentage points).

For a binomial proportion `p` over `n` independent trials, the standard
error is

    SE(p) = sqrt(p * (1 - p) / n)

At the suite-wide level (`n = 30`, `p ≈ 0.9` for a healthy model) that's

    SE = sqrt(0.9 * 0.1 / 30) ≈ 0.055  →  ±5.5pp at 1σ

A 2pp absolute regression is well inside that 1σ band, so a single run
**cannot** detect a 2pp drop at p < 0.05. The minimum detectable effect
(MDE) for one run at p < 0.05 (z = 1.96) is roughly 1.96 * SE ≈ 11pp.

To detect a 2pp regression at p < 0.05 we need either:

1. **More runs.** With `k` runs the SE shrinks by `sqrt(k)`. To bring the
   1.96σ bound to ≤ 2pp at `p ≈ 0.9` we need

       1.96 * sqrt(0.9 * 0.1 / (30 * k)) ≤ 0.02
       →  k ≥ 1.96² * 0.09 / (30 * 0.02²) ≈ 29 runs

   so roughly **30 deterministic runs** of the suite per candidate, or

2. **A bigger suite.** Inverting the same inequality for `n` at `k = 1`
   gives `n ≥ 1.96² * 0.09 / 0.02² ≈ 865` tasks. A 1000-task suite
   detects 2pp at p < 0.05 in a single run.

Per-category power is worse: the math sub-suite (`n = 10`) has
`SE ≈ 0.095` at `p = 0.9` so its single-run MDE is ~19pp. **Anything
smaller than ~20pp on a single category needs multiple runs to debias.**

The suite is intentionally small for CI cost (the smoke job runs in
under a second). The drift job is meant to consume canary traffic
samples in addition to the golden suite — that's where statistical
power comes from in production. The committed baseline in
`eval/baselines/golden_v1_fake.json` records `n_tasks` so the math is
auditable.

## What's deliberately not here

- **Distributed circuit breaker.** In-process is enough for a single-pod
  deployment. A sketch of a Redis-backed sliding window lives in the roadmap.
- **Real-traffic canary scoring.** The `canary_results` table and the drift
  detection arithmetic are present; a production sampler (route 1% of traffic
  to the candidate model and diff against baseline output) is not. The eval
  CLI is the offline analogue.
- **Per-request-token billing reconciliation.** `cost_usd` in `request_log` is
  estimated from the price table at request time. For invoicing-grade numbers
  pull provider usage exports nightly and reconcile.
- **Admin dashboard (Next.js).** Out of scope; REST + cursor pagination is
  enough for `curl` and the Python client.
- **GraphQL admin endpoint.** REST keeps the surface small.
- **K8s manifests / Helm chart.** The container builds; `docker-compose` runs
  the local stack. Production topology is intentionally left to the operator.
