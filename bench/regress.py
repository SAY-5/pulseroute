"""Bench regression gate.

Compares a freshly produced ``bench/results/<ts>.json`` against a baseline file
and exits non-zero if any tracked metric drifts beyond the configured
threshold (default 30%).

The metric set is intentionally small and stable — we want to fail loudly on
regressions in the gateway-added latency tail, the cache hit rate, and the
routed-vs-pinned cost ratio. Any other metric is informational.

Run
---
    python bench/regress.py BASELINE.json FRESH.json
    python bench/regress.py BASELINE.json FRESH.json --threshold 0.30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Each entry: (json_path_dotted, "higher_is_worse" | "lower_is_worse" | "absolute_drift")
# higher_is_worse: regress if fresh is X% > baseline (latency)
# lower_is_worse: regress if fresh is X% < baseline (cache hit rate)
# absolute_drift: regress if abs(fresh - baseline) / baseline > threshold (cost)
TRACKED: list[tuple[str, str]] = [
    ("summary.latency_ms.p50_ms", "higher_is_worse"),
    ("summary.latency_ms.p95_ms", "higher_is_worse"),
    ("summary.latency_ms.p99_ms", "higher_is_worse"),
    ("summary.cache.hit_rate_overall", "lower_is_worse"),
    ("summary.cache.hit_rate_on_dups", "lower_is_worse"),
    ("summary.cost.savings_pct", "lower_is_worse"),
]


def _get(obj: dict[str, Any], dotted: str) -> float:
    cur: Any = obj
    for part in dotted.split("."):
        cur = cur[part]
    return float(cur)


def compare(baseline: dict[str, Any], fresh: dict[str, Any], threshold: float) -> list[str]:
    """Return a list of regression strings; empty list = pass."""
    failures: list[str] = []
    for path, mode in TRACKED:
        try:
            b = _get(baseline, path)
            f = _get(fresh, path)
        except (KeyError, TypeError) as exc:
            failures.append(f"missing metric {path}: {exc}")
            continue
        if b == 0.0:
            continue  # cannot ratio-compare against zero
        delta = (f - b) / b
        if mode == "higher_is_worse" and delta > threshold:
            failures.append(
                f"{path}: regressed by {delta * 100:.1f}% (baseline={b}, fresh={f}, "
                f"threshold=+{threshold * 100:.0f}%)"
            )
        elif mode == "lower_is_worse" and delta < -threshold:
            failures.append(
                f"{path}: regressed by {delta * 100:.1f}% (baseline={b}, fresh={f}, "
                f"threshold=-{threshold * 100:.0f}%)"
            )
        elif mode == "absolute_drift" and abs(delta) > threshold:
            failures.append(
                f"{path}: drifted by {delta * 100:.1f}% (baseline={b}, fresh={f}, "
                f"threshold={threshold * 100:.0f}%)"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PulseRoute bench regression gate")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("fresh", type=Path)
    parser.add_argument("--threshold", type=float, default=0.30)
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text())
    fresh = json.loads(args.fresh.read_text())

    failures = compare(baseline, fresh, args.threshold)
    if failures:
        print(f"# bench-regress FAIL ({len(failures)} regression(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("# bench-regress PASS")
    print(f"  baseline = {args.baseline}")
    print(f"  fresh    = {args.fresh}")
    print(f"  threshold= {args.threshold * 100:.0f}%")
    for path, _mode in TRACKED:
        try:
            b = _get(baseline, path)
            f = _get(fresh, path)
            print(f"  {path}: baseline={b}, fresh={f}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
