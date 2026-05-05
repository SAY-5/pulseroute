# PulseRoute — Roadmap

Tracked deferrals. Each item links to the rationale in code or docs.

## Deferred — likely future sprints

### HDR histogram port — `packages/shared/hdr.py`

The Prometheus histogram in `services/gateway/app/metrics.py` uses 25 ms
bucket granularity (see the bucket list near `GATEWAY_ADDED_LATENCY`). At
the gateway's measured P95 (~9 ms on M-series Mac) buckets that coarse
make sub-bucket variance invisible — fine for production SLOs, not fine
for performance-regression analysis at the tail. Plan: port HdrHistogram
to a small `packages/shared/hdr.py` module and dual-emit; Prometheus
keeps the buckets, HdrHistogram emits percentiles directly.

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

### Sprint 2 (this sprint)
- Math eval suite expanded from 10 hand-written problems to 200 sampled
  GSM8K test-split problems (deterministic seed=42).
- Real-traffic canary sampler (`pulseroute canary run`) with deterministic
  sampling, in-memory + HTTP ClickHouse client, stub LLM judge for tests,
  Slack webhook env-gated.
- `canary_results` ClickHouse migration.
- Drift-power section recalculated for N=200; combined math+canary window
  of 1000 clears the README's 2pp@p<0.05 promise.
