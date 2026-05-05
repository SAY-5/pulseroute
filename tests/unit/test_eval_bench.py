"""Multi-model bench artifact + Pareto rendering tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pulseroute_eval.bench import _cost_per_task, bench_models, write_artifact
from pulseroute_eval.runner import deterministic_fake_outputs


@pytest.mark.asyncio
async def test_bench_models_returns_expected_schema():
    payload = await bench_models(["fake-small", "fake-large"])
    assert payload["suite"] == "golden_v1"
    assert payload["provider"] == "fake"
    # 200 GSM8K math problems + 5 code + 5 refusal + 10 RAG = 220.
    assert payload["n_tasks"] == 220
    assert {m["model"] for m in payload["models"]} == {"fake-small", "fake-large"}
    for m in payload["models"]:
        assert m["accuracy"] == pytest.approx(1.0)
        assert m["refusal_compliance"] == pytest.approx(1.0)
        assert set(m["by_category"]) == {"math", "code", "refusal", "rag"}
        assert m["cost_per_task_usd"] > 0


@pytest.mark.asyncio
async def test_bench_artifact_round_trip(tmp_path: Path):
    payload = await bench_models(["fake-small"])
    out = tmp_path / "baseline.json"
    write_artifact(payload, out)
    reloaded = json.loads(out.read_text())
    assert reloaded["models"][0]["model"] == "fake-small"


def test_cost_per_task_uses_price_table():
    # fake-large should be strictly more expensive per task than fake-small
    # because the price table assigns higher per-1k rates to fake-large.
    faked = deterministic_fake_outputs()
    small = _cost_per_task("fake-small", faked)
    large = _cost_per_task("fake-large", faked)
    assert large > small > 0
