"""Render eval/baselines/pareto.md from a baseline artifact JSON.

Pareto frontier here means "no other model is both cheaper and at least as
accurate". With FakeProvider all models share scripted accuracy, so the
frontier degenerates to the cheapest acceptable variant — we mark it bold
anyway so the format is consistent with future BYOK runs.

Usage:
    python scripts/gen_pareto_md.py eval/baselines/golden_v1_fake.json \
        eval/baselines/pareto.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HEADER = """# Pareto frontier — golden_v1 (FakeProvider)

> This is a hermetic FakeProvider baseline that exercises the eval pipeline
> end-to-end and asserts no crashes. Quality numbers are scripted, not
> measured. Live Pareto requires BYOK; see the project README.

Suite: `golden_v1`  |  Provider: `fake`  |  Tasks: {n_tasks}
Generated at: `{generated_at}`  |  Git SHA: `{git_sha}`
"""

TABLE_HEADER = (
    "| model | accuracy | math | code | refusal | rag | refusal_compliance "
    "| p95_latency_ms | cost_per_task_usd |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
)


def _is_pareto(candidate: dict[str, Any], all_models: list[dict[str, Any]]) -> bool:
    """A model is Pareto-optimal if no other model is at least as accurate AND
    strictly cheaper, OR equal cost but strictly more accurate."""
    c_acc = candidate["accuracy"]
    c_cost = candidate["cost_per_task_usd"]
    for other in all_models:
        if other["model"] == candidate["model"]:
            continue
        o_acc = other["accuracy"]
        o_cost = other["cost_per_task_usd"]
        better_or_equal_acc = o_acc >= c_acc
        cheaper_or_equal_cost = o_cost <= c_cost
        strictly_better = (o_acc > c_acc and o_cost <= c_cost) or (
            o_acc >= c_acc and o_cost < c_cost
        )
        if better_or_equal_acc and cheaper_or_equal_cost and strictly_better:
            return False
    return True


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _fmt_cost(v: float) -> str:
    if v == 0:
        return "$0"
    if v < 1e-4:
        return f"${v:.2e}"
    return f"${v:.6f}"


def render(artifact: dict[str, Any]) -> str:
    rows = [HEADER.format(**artifact), TABLE_HEADER.rstrip()]
    models = artifact["models"]
    for m in models:
        bc = m.get("by_category", {})
        cells = [
            m["model"],
            _fmt_pct(m["accuracy"]),
            _fmt_pct(bc.get("math", 0.0)),
            _fmt_pct(bc.get("code", 0.0)),
            _fmt_pct(bc.get("refusal", 0.0)),
            _fmt_pct(bc.get("rag", 0.0)),
            _fmt_pct(m["refusal_compliance"]),
            str(m["p95_latency_ms"]),
            _fmt_cost(m["cost_per_task_usd"]),
        ]
        if _is_pareto(m, models):
            cells = [f"**{c}**" for c in cells]
        rows.append("| " + " | ".join(cells) + " |")
    rows.append("")
    rows.append(
        "**Bold** rows are on the cheapest-acceptable-quality Pareto frontier "
        "(no other model is both cheaper and at least as accurate)."
    )
    rows.append("")
    rows.append(
        "Reproduce: `make bench-eval` (writes a fresh artifact under "
        "`eval/runs/<timestamp>.json` and refreshes `eval/baselines/golden_v1_fake.json`)."
    )
    rows.append("")
    return "\n".join(rows)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: gen_pareto_md.py <artifact.json> <out.md>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    artifact = json.loads(src.read_text())
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(artifact))
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
