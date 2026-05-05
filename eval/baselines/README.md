# eval/baselines

Committed reference artifacts for the `golden_v1` suite.

## Files

| File | Purpose |
|---|---|
| `golden_v1_fake.json` | Latest hermetic FakeProvider run of the 30-task golden suite, both `fake-small` and `fake-large`. Drives the README results table. |
| `pareto.md` | Rendered Pareto-style table with the cheapest-acceptable-quality frontier in **bold**. Generated from `golden_v1_fake.json`. |

## What this is

> This is a hermetic FakeProvider baseline that exercises the eval pipeline
> end-to-end and asserts no crashes. Quality numbers are scripted, not
> measured. Live Pareto requires BYOK; see the project README.

The runner uses `deterministic_fake_outputs()` (see
`services/eval-runner/pulseroute_eval/runner.py`) so every fake "model"
produces the same scripted answer per task. That answer is crafted to score
1.0 against the suite, so the artifact is useful for:

- Confirming the eval pipeline still runs end-to-end (CI's `eval-smoke` job).
- Smoke-testing changes to scoring, suites, or the runner.
- Pinning the wire format of the artifact so downstream consumers (the README
  table generator, future drift jobs) don't break.

It is **not** useful for comparing models on real prompts. Both fake models
get 100% on every category by construction; the only meaningful difference in
this baseline is the price-table-derived `cost_per_task_usd`.

## Cost methodology

`cost_per_task_usd` is computed from `MODEL_PRICES` (in
`packages/router/pulseroute_router/cost.py`) by summing
`prompt_tokens * input_per_1k + output_tokens * output_per_1k` across the
suite, divided by the task count. Token counts use the same
`estimate_tokens` helper the router uses for live cost estimates
(~4 chars/token), so the number reflects the model's published price under
this suite's prompt distribution.

## Reproducing

```bash
make bench-eval
```

This refreshes `golden_v1_fake.json`, copies the same payload into a
timestamped `eval/runs/<timestamp>.json`, and re-renders `pareto.md`. The
artifact is committed; the run files in `eval/runs/` are local-only (see
`.gitignore`).

## Live (BYOK)

To run the suite against real providers:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
pulseroute-eval run --suite golden_v1 \
    --provider openai --model gpt-4o-mini \
    --output eval/runs/$(date -u +%Y%m%dT%H%M%SZ)-gpt4o-mini.json
```

The eval CLI's `--provider openai|anthropic` paths are stubs in the current
scaffold — wire them up against `pulseroute_router.providers.openai`/`.anthropic`
before running. See the README for the full BYOK flow.
