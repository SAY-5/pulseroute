# PulseRoute

PulseRoute is an OpenAI-compatible HTTP gateway in front of multiple LLM providers.
It does policy-driven routing across providers, semantic caching, per-tenant cost
ceilings, per-(provider, model) circuit breakers, and runs an eval suite as part of
CI. The hot path is FastAPI on uvicorn; analytics live in ClickHouse.

## What this studies

- **Routing under uncertainty.** Picking a model is a multi-objective decision
  (cost, quality, latency, availability) and the right answer changes per
  tenant. The router compiles a tenant context plus a named policy into an
  ordered candidate list, then walks it honouring breaker state.
- **Semantic caching that doesn't lie.** Cache hits are gated on cosine
  similarity above a configurable threshold (default 0.97) of a normalised
  prompt fingerprint, scoped per-tenant. Tenants can opt out per request.
- **Hermetic eval-as-CI.** A 30-task golden suite scored across math, code,
  refusal, and grounded QA runs against `FakeProvider` on every PR — the
  pipeline is exercised end-to-end without burning real provider credits.
- **Failure-mode discipline.** User-facing requests fail fast with structured
  errors (no in-request retry); only async non-user-facing work uses retries
  with DLQ. The breaker state machine is unit-tested.

## Module table

| Path | Purpose |
|---|---|
| `services/gateway/` | FastAPI app, OpenAI-compatible API, SSE streaming, admin |
| `services/eval-runner/` | Click CLI, golden suite, scoring, drift detection |
| `packages/router/` | `ChatProvider` Protocol, providers (OpenAI/Anthropic/Fake), routing policies, circuit breaker, cost model |
| `packages/policies/` | Cost ceilings, content filter, PII redaction |
| `packages/cache/` | Prompt normalisation, embeddings, Redis-backed semantic cache |
| `packages/shared/` | Pydantic types, OTel bootstrap, settings |
| `packages/clients/python/` | Drop-in OpenAI-shaped client wrapper |
| `infra/clickhouse/` | Analytics schema (request_log, hourly rollup MV, eval_results) |
| `infra/dashboards/` | Grafana JSON: overview, router, providers |
| `services/gateway/migrations/` | Alembic control-plane migrations |

## Quickstart

```bash
make install          # editable install + dev deps
make test             # unit tests (66+ tests, ~0.5s)
make typecheck        # mypy strict on router + policies
pulseroute-eval smoke # 30-task golden suite via FakeProvider
```

To run the full local stack:

```bash
make up               # docker compose: postgres, redis, clickhouse, otel, grafana
make migrate          # Alembic schema bootstrap
python scripts/clickhouse_migrate.py   # ClickHouse schema
make seed             # 3 tenants + 50k synthetic request_log rows
make dev              # uvicorn on :8080
```

Then call it like the OpenAI API:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer pr_test_quality" \
  -H "Content-Type: application/json" \
  -d '{"model":"fake-large","messages":[{"role":"user","content":"hello"}]}' | jq
```

Or with the Python client (drop-in for the OpenAI SDK):

```python
from pulseroute_client import OpenAI
c = OpenAI(api_key="pr_test_quality", base_url="http://localhost:8080/v1")
r = c.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hi"}],
)
```

## Reproducibility

```bash
# Eval suite (FakeProvider, deterministic)
pulseroute-eval run --suite golden --provider fake --model fake-large

# Hermetic perf bench (no docker; uses ASGI transport)
python scripts/bench_asgi.py --n 200 --concurrency 16 --p95-budget-ms 200

# Live perf bench (requires `make up && make dev`)
bash scripts/bench.sh
```

## Design targets

These are the gateway's stated targets. Numbers below come from the in-process
ASGI bench (`scripts/bench_asgi.py`) on a quiet workstation; production figures
require running `bench.sh` against the docker stack.

| Metric | Target | Local in-process |
|---|---|---|
| P50 added gateway latency | < 40 ms | ~3 ms |
| P95 added gateway latency (excl. upstream) | < 120 ms | ~5 ms |
| Semantic cache hit rate on repeat workloads | >= 35% | (workload-dependent; see `tests/unit/test_cache.py`) |
| Cost reduction vs single-provider baseline | >= 25% | <TBD on workload mix> |
| Drift alarm | fires on > 2% regression on canary suite | implemented; production threshold configurable |

CI runs `bench_asgi.py` with the P95 budget relaxed to 200 ms to absorb GitHub
Actions runner variance.

## Architecture (request path)

```
client
  |
  v
+--------------------+
|  RequestId mw      |
+--------------------+
  |
  v
+--------------------+        miss/skip
| Semantic cache     |---------+
| (fp + cosine)      |         |
+--------------------+         v
  | hit                  +-----------------+
  v                      | Router.decide   |   policy: cost_capped | quality_first |
+--------+               |  - rank cands   |   latency_first | cheapest_first
| return |<-----+        |  - skip OPEN    |
+--------+      |        |    breakers     |
                |        +-----------------+
                |              |
                |              v
                |        +-----------------+         retryable: false
                |        | provider.call   |--->  upstream_unavailable (502)
                |        |  - record       |
                |        |    success/fail |
                |        +-----------------+
                |              |
                +<-------------+ store in cache (unless no_cache)
                v
            response
```

## Tests

- **66+ unit tests, ~0.5s.** Routing-policy decision tables (10 scenarios),
  cache key normalisation, cost calculator across the full price table,
  circuit breaker state machine, gateway happy-path + failover + streaming +
  admin pagination.
- **Coverage.** `packages/router` and `packages/policies` are gated at 85%
  (currently ~91%).
- **Provider HTTP** is mocked with `respx` so no real keys are needed.
- **`migrate-check`** runs Alembic + ClickHouse migrations from scratch
  against the GHA postgres+clickhouse services on every push.
- **`perf-smoke`** runs an in-process bench gated at P95 < 200 ms.
- **`eval-smoke`** runs the 30-task golden suite via FakeProvider and asserts
  no crashes (the deterministic crafted outputs hit every scoring path).

## Layout

```
.
|-- services/
|   |-- gateway/             # FastAPI, OpenAI-compatible API
|   |-- eval-runner/         # Click CLI for the golden suite
|-- packages/
|   |-- router/              # provider Protocol, policies, breaker, cost
|   |-- policies/            # cost ceilings, content filter, PII
|   |-- cache/               # semantic cache + normalisation
|   |-- shared/              # types, OTel, settings
|   |-- clients/python/      # drop-in OpenAI-shaped client
|-- infra/
|   |-- clickhouse/          # analytics schema
|   |-- dashboards/          # Grafana JSON
|   |-- prometheus.yml
|   |-- otel-collector.yaml
|-- scripts/
|   |-- seed.py              # 3 tenants + 50k synthetic rows
|   |-- bench.sh             # live perf smoke
|   |-- bench_asgi.py        # hermetic in-process bench
|   |-- clickhouse_migrate.py
|-- tests/
|   |-- unit/                # all tests run without docker
|   |-- integration/         # gated on RUN_INTEGRATION=1 (not yet wired)
|-- .github/workflows/ci.yml
|-- docker-compose.yml
|-- Makefile
|-- pyproject.toml
```

## What this is not

- **Not a billing system.** Cost numbers are estimated from the `MODEL_PRICES`
  table for routing decisions and rough accounting only. Real billing should
  reconcile against provider invoices.
- **Not a moderation provider.** Content filter is a thin blocklist + length
  guard. Wire to a real moderation endpoint for production.
- **No real-traffic canary.** The `canary_results` table and drift scoring
  exist; the production wiring (sampler + Slack alert) is left as a stub.
- **No GraphQL admin.** The admin API is REST + cursor pagination only.
- **No request-level retries.** User-facing requests fail fast with a
  structured error; clients decide whether to retry. Async eval/canary jobs do
  retry with DLQ.

## Known issues

- The `services/admin` Next.js dashboard is intentionally omitted — the REST
  admin API is sufficient for the demo.
- ClickHouse archival to S3 cold storage is a documented stub, not implemented.
- The Anthropic streaming parser handles only the `text_delta` event shape; the
  full Anthropic event taxonomy is not exhaustive.

## License

MIT — see `LICENSE`.
