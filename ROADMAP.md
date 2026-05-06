# PulseRoute — Roadmap

Tracked deferrals. Each item links to the rationale in code or docs.

## Deferred — likely future sprints

### Distributed circuit breaker

The current breaker (`packages/router/pulseroute_router/breaker.py`) is
in-process, sized for a single-pod deployment. Multi-pod gateways
benefit from a Redis-backed sliding window so one pod's failure
observations propagate. ARCHITECTURE.md §"Circuit breaker math" notes
why we deliberately keep it in-process for now.

### ClickHouse → S3 archival

Old partitions in `pulseroute.request_log` get pruned monthly by the
partition-by clause but no cold-storage tier exists. Plan: nightly
`ALTER TABLE … MOVE PARTITION` to an S3-backed disk, or a separate
`archived_request_log` table with `MergeTree(disk='cold')`.

## Out of scope

These were considered and rejected for this iteration; reopen only if a
concrete user need surfaces.

### Next.js admin UI — `services/admin/`

Out of scope. The REST admin API plus `curl` and the Python client cover
every demo path. Adding a SPA doubles the build surface for marginal
demo value.

### GraphQL admin endpoint

Out of scope. The admin surface is small; cursor-paginated REST is
simpler to test, document, and version. GraphQL would be reasonable if
the admin client surface grew an order of magnitude.

### Kubernetes manifests / Helm chart

Out of scope. The container builds; `docker-compose` runs the local
stack. Production topology — replicas, secrets, network policy — is
operator territory and varies enough that a vendored manifest would
mostly be wrong.

## Completed

### Sprint 0
- Eval-as-CI green on every PR (FakeProvider).
- Pareto frontier artifact + `make bench-eval`.
- Drift-detection statistical-power note in ARCHITECTURE.md.

### Sprint 2
- Math eval suite expanded from 10 hand-written problems to 200 sampled
  GSM8K test-split problems (deterministic seed=42).
- Real-traffic canary sampler (`pulseroute canary run`) with deterministic
  sampling, in-memory + HTTP ClickHouse client, stub LLM judge for tests,
  Slack webhook env-gated.
- `canary_results` ClickHouse migration.
- Drift-power section recalculated for N=200; combined math+canary window
  of 1000 clears the README's 2pp@p<0.05 promise.

### Sprint 4 (this sprint)
- HDR log-bucketed histogram (`packages/shared/pulseroute_shared/hdr.py`)
  ported from streamflow's `LatencyHistogram` and orderbook's
  `histogram.hpp`. Microsecond resolution, ~6% per-bucket relative error,
  threading.Lock-guarded counter array.
- Gateway-added latency and cache lookup latency now record into HDR;
  `/metrics` scrape emits both as Prometheus `histogram` blocks.
  End-to-end request latency keeps the existing Prometheus `Histogram`.
