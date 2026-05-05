# Pareto frontier — golden_v1 (FakeProvider)

> This is a hermetic FakeProvider baseline that exercises the eval pipeline
> end-to-end and asserts no crashes. Quality numbers are scripted, not
> measured. Live Pareto requires BYOK; see the project README.

Suite: `golden_v1`  |  Provider: `fake`  |  Tasks: 30
Generated at: `2026-05-05T22:06:54+00:00`  |  Git SHA: `10b7e18`

| model | accuracy | math | code | refusal | rag | refusal_compliance | p95_latency_ms | cost_per_task_usd |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **fake-small** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **0** | **$2.60e-07** |
| fake-large | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 0 | $2.62e-06 |

**Bold** rows are on the cheapest-acceptable-quality Pareto frontier (no other model is both cheaper and at least as accurate).

Reproduce: `make bench-eval` (writes a fresh artifact under `eval/runs/<timestamp>.json` and refreshes `eval/baselines/golden_v1_fake.json`).
