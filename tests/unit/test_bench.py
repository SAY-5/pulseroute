"""Smoke test for the bench harness.

Runs a small replay (1000 requests) end-to-end and asserts the JSON artifact
has the expected shape. We do NOT assert on the actual numbers — those are
machine-dependent and reported in the README from a real bench run."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# bench/ lives at the repo root and isn't an installed package, so load by
# path. This keeps the harness a stand-alone program rather than an importable
# library shipped with the wheel.
_BENCH_PATH = Path(__file__).resolve().parents[2] / "bench" / "bench.py"
_spec = importlib.util.spec_from_file_location("bench_bench", _BENCH_PATH)
assert _spec and _spec.loader
bench_mod = importlib.util.module_from_spec(_spec)
sys.modules["bench_bench"] = bench_mod
_spec.loader.exec_module(bench_mod)


def test_workload_is_deterministic():
    a = bench_mod.generate_workload(500, seed=42)
    b = bench_mod.generate_workload(500, seed=42)
    assert [(r.idx, r.bucket, r.is_duplicate, r.tenant_key, r.prompt) for r in a] == [
        (r.idx, r.bucket, r.is_duplicate, r.tenant_key, r.prompt) for r in b
    ]


def test_workload_mix_within_tolerance():
    n = 5000
    workload = bench_mod.generate_workload(n, seed=42)
    short = sum(1 for r in workload if r.bucket == "short")
    medium = sum(1 for r in workload if r.bucket == "medium")
    long = sum(1 for r in workload if r.bucket == "long")
    dups = sum(1 for r in workload if r.is_duplicate)
    # Loose tolerances; the exact counts are deterministic and tested above.
    assert 0.65 * n <= short <= 0.75 * n
    assert 0.20 * n <= medium <= 0.30 * n
    assert 0.02 * n <= long <= 0.08 * n
    assert 0.27 * n <= dups <= 0.33 * n


@pytest.mark.asyncio
async def test_bench_runs_at_smoke_scale(tmp_path: Path):
    agg, results_path, table = await bench_mod.run_bench(n=1000, seed=42, out_dir=tmp_path)
    assert results_path.exists(), "results JSON should be written"
    assert results_path.parent == tmp_path

    payload = json.loads(results_path.read_text())
    # Required top-level keys.
    assert set(payload.keys()) >= {"meta", "summary"}
    assert set(payload["meta"].keys()) >= {
        "timestamp",
        "n_requests",
        "seed",
        "workload_mix",
        "duplicate_ratio",
        "pinned_model",
    }
    assert set(payload["summary"].keys()) >= {
        "n_requests",
        "n_ok",
        "latency_ms",
        "cache",
        "route_reasons",
        "errors_per_upstream",
        "cost",
    }

    # Latency keys.
    assert set(payload["summary"]["latency_ms"].keys()) >= {
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "p999_ms",
        "max_ms",
    }

    # Cache keys.
    assert set(payload["summary"]["cache"].keys()) >= {
        "hit_rate_overall",
        "hit_rate_on_dups",
        "hit_rate_on_uniques",
    }

    # Cost keys.
    assert set(payload["summary"]["cost"].keys()) >= {
        "routed_usd",
        "pinned_usd",
        "savings_pct",
    }

    # Sanity: n_ok should equal n_requests (FakeProvider does not raise).
    assert payload["summary"]["n_ok"] == 1000
    # Stdout table is non-empty and includes the header.
    assert "pulseroute bench" in table
    assert "gateway-added latency" in table

    # Aggregate object matches the JSON we wrote.
    assert agg.n_requests == 1000
    assert agg.n_ok == 1000
