# Pareto frontier — golden_v1 (FakeProvider)

> This is a hermetic FakeProvider baseline that exercises the eval pipeline
> end-to-end and asserts no crashes. Quality numbers are scripted, not
> measured. Live Pareto requires BYOK; see the project README.

Suite: `golden_v1`  |  Provider: `fake`  |  Tasks: 220
Generated at: `2026-05-05T22:18:37+00:00`  |  Git SHA: `bde8127`

Math sub-suite: 200 problems sampled deterministically from GSM8K test split
(seed=42); see `eval/suites/golden_v1/math.yaml` and `scripts/sample_gsm8k.py`.

| model | accuracy | math | code | refusal | rag | refusal_compliance | p95_latency_ms | cost_per_task_usd |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **fake-small** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **0** | **$7.40e-07** |
| fake-large | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 0 | $7.38e-06 |

**Bold** rows are on the cheapest-acceptable-quality Pareto frontier (no other model is both cheaper and at least as accurate).

Reproduce: `make bench-eval` (writes a fresh artifact under `eval/runs/<timestamp>.json` and refreshes `eval/baselines/golden_v1_fake.json`).
