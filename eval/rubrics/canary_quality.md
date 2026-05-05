# Canary quality rubric (golden_v1)

Used by `pulseroute canary run` to score replayed responses on both arms
(stable + treatment) against the same LLM judge. The judge is configured
once per canary run and applied to both arms so judge bias cancels out at
the win-rate level.

## Inputs the judge sees

For each sampled `request_log` row the judge gets:

- The original user prompt(s) — `messages` list, OpenAI shape.
- The retrieved context, if any (RAG payloads from the gateway request).
- The candidate response — text only, no model identity.
- The original `request_log` row's `model` is **not** revealed to avoid
  brand bias; the judge sees `Response A` and `Response B` only.

## Scoring axes

The judge emits a single integer in `[0, 5]` per response on each axis;
the per-arm score is the unweighted mean of axis scores divided by 5
(so it's in `[0.0, 1.0]`).

| Axis | What "5" looks like | What "0" looks like |
|---|---|---|
| **Helpfulness** | Directly answers the user's question, no padding. | Refuses unprompted, dodges, or rambles without resolving the ask. |
| **Correctness** | Every factual claim is verifiable; arithmetic is exact. | Hallucinated facts, wrong arithmetic, or outdated information. |
| **Faithfulness to context** | Every retrieved-context claim is cited or paraphrased without distortion. (If no context was retrieved, this axis is omitted.) | Contradicts the retrieved context, fabricates citations. |
| **Tone fit** | Matches the prompt's register (technical / conversational / formal). | Wrong register, e.g. casual reply to an enterprise legal question. |

## Tie-breaking

A single canary judgment is one of `win_treatment | win_stable | tie`:

- `win_treatment` if `score(treatment) - score(stable) > 0.05`
- `win_stable` if `score(stable) - score(treatment) > 0.05`
- otherwise `tie`

The 0.05 dead-zone (5% of the [0,1] score range) prevents judge noise from
generating spurious wins on indistinguishable outputs.

## Aggregation

Over a window of N judgments:

```
win_rate_treatment = wins / N
loss_rate_treatment = losses / N
margin = wins - losses    # signed
```

Alert fires when `loss_rate - win_rate > 0.02` for `N >= 1000`. The 2%
threshold matches the README's drift-detection promise; the 1000-sample
floor is from the suite-wide statistical-power calculation (see
ARCHITECTURE.md §drift-statistical-power).

## Determinism

The judge call is non-deterministic in production. To make reruns
reproducible we:

1. Persist `(judgment, score_stable, score_treatment, judge_model)` in
   the `canary_results` table per sampled request.
2. The sampler itself is fully deterministic given
   `(window_start, window_end, sample_rate, seed)` — re-sampling the
   same window picks the same `request_log` rows so investigations can
   recheck specific judgments without re-querying the full window.

A stub judge — `pulseroute_eval.canary.StubLLMJudge` — implements the
same interface and is used in unit tests. It picks a deterministic
score from a hash of the response text so test outcomes are stable
across runs.
