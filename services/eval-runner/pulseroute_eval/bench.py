"""Multi-model bench that produces a Pareto-style summary artifact.

Runs the golden suite against each requested fake model, then computes:

    accuracy, by_category accuracy, refusal_compliance, p95_latency_ms,
    cost_per_task_usd

Cost is derived from `MODEL_PRICES` and the request's prompt + the recorded
output token counts — so it tracks the model's actual price even when the
runner is using `faked_outputs` to short-circuit the provider call.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulseroute_router.cost import MODEL_PRICES, estimate_tokens
from pulseroute_router.providers.fake import FakeProvider

from pulseroute_eval.runner import SuiteResult, deterministic_fake_outputs, run_suite
from pulseroute_eval.suites import GOLDEN_SUITE


@dataclass
class ModelSummary:
    model: str
    n_tasks: int
    accuracy: float
    by_category: dict[str, float]
    refusal_compliance: float
    p95_latency_ms: int
    cost_per_task_usd: float


def _refusal_compliance(result: SuiteResult) -> float:
    refusal = [t for t in result.tasks if t.category == "refusal"]
    if not refusal:
        return 0.0
    return sum(t.score for t in refusal) / len(refusal)


def _cost_per_task(model: str, faked_outputs: dict[str, str]) -> float:
    """Average USD cost per task using MODEL_PRICES and the configured prompts.

    Prompt tokens are estimated from each task's prompt + a fixed system
    preamble (matching the runner). Output tokens come from the deterministic
    faked output so the number is reproducible across runs.
    """
    price = MODEL_PRICES.get(model)
    if price is None:
        return float("nan")
    system_tokens = estimate_tokens("You are a careful assistant. Answer briefly.")
    total = 0.0
    for task in GOLDEN_SUITE:
        prompt_tokens = system_tokens + estimate_tokens(task.prompt)
        output_text = faked_outputs.get(task.id, "")
        output_tokens = estimate_tokens(output_text) if output_text else 64
        total += (
            prompt_tokens * price.input_per_1k / 1000.0
            + output_tokens * price.output_per_1k / 1000.0
        )
    return total / len(GOLDEN_SUITE)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


async def bench_models(models: list[str]) -> dict[str, Any]:
    """Run the golden suite against each model and assemble a summary dict."""
    faked = deterministic_fake_outputs()
    summaries: list[ModelSummary] = []
    for model in models:
        provider = FakeProvider()
        result = await run_suite(
            provider,
            model=model,
            tasks=GOLDEN_SUITE,
            concurrency=8,
            faked_outputs=faked,
        )
        summaries.append(
            ModelSummary(
                model=model,
                n_tasks=len(result.tasks),
                accuracy=round(result.accuracy, 4),
                by_category={k: round(v, 4) for k, v in result.by_category.items()},
                refusal_compliance=round(_refusal_compliance(result), 4),
                p95_latency_ms=result.p95_latency_ms,
                cost_per_task_usd=round(_cost_per_task(model, faked), 8),
            )
        )
    return {
        "suite": "golden_v1",
        "provider": "fake",
        "n_tasks": len(GOLDEN_SUITE),
        "git_sha": _git_sha(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": (
            "Hermetic FakeProvider baseline. Quality numbers come from scripted "
            "deterministic outputs in deterministic_fake_outputs(); they exercise "
            "the eval pipeline but are not measurements of any real model."
        ),
        "models": [asdict(s) for s in summaries],
    }


def write_artifact(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
