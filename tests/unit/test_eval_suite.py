"""Structural assertions on the YAML-backed math suite.

These keep drift detection's statistical-power calculations honest: if anyone
trims the suite below 200 problems or changes the id format, this test fails
loudly rather than the README's MDE numbers silently going stale.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml
from pulseroute_eval.suites import GOLDEN_SUITE, MATH_SUITE, MATH_SUITE_YAML

ID_RE = re.compile(r"^gsm8k_test_\d+$")


def test_math_suite_yaml_exists():
    assert MATH_SUITE_YAML.exists(), (
        f"missing {MATH_SUITE_YAML}; run `python scripts/sample_gsm8k.py`"
    )


def test_math_suite_has_at_least_200_problems():
    assert len(MATH_SUITE) >= 200, (
        f"math suite shrank to {len(MATH_SUITE)}; drift-detection power calc assumes N>=200"
    )


def test_math_suite_problem_ids_are_unique_and_well_formed():
    ids = [t.id for t in MATH_SUITE]
    assert len(set(ids)) == len(ids), "duplicate ids in math suite"
    bad = [i for i in ids if not ID_RE.match(i)]
    assert not bad, f"ids must match gsm8k_test_<int>: bad={bad[:5]}"


def test_every_math_problem_has_a_numeric_answer():
    bad: list[str] = []
    for task in MATH_SUITE:
        assert task.expected is not None
        # GSM8K answers are integers or decimals; allow leading minus and
        # commas.
        cleaned = task.expected.replace(",", "")
        try:
            float(cleaned)
        except ValueError:
            bad.append(task.id)
    assert not bad, f"non-numeric answers: {bad[:5]}"


def test_math_problems_are_in_math_category():
    for task in MATH_SUITE:
        assert task.category == "math", task.id


def test_golden_suite_includes_every_math_problem():
    math_ids_in_golden = {t.id for t in GOLDEN_SUITE if t.category == "math"}
    math_ids = {t.id for t in MATH_SUITE}
    assert math_ids_in_golden == math_ids


def test_yaml_schema_round_trip():
    """Re-parsing the on-disk YAML matches the in-memory MATH_SUITE.
    Catches drift between the loader and the file format.
    """
    raw = yaml.safe_load(MATH_SUITE_YAML.read_text(encoding="utf-8"))
    assert raw["suite"] == "golden_v1"
    assert raw["category"] == "math"
    assert raw["n_problems"] == len(raw["problems"])
    assert raw["n_problems"] == len(MATH_SUITE)


def test_sample_script_is_documented():
    """The helper script must say it's a build-time tool, not a runtime path."""
    script = pathlib.Path(MATH_SUITE_YAML).resolve().parents[3] / "scripts" / "sample_gsm8k.py"
    text = script.read_text(encoding="utf-8")
    assert "live network is not required at eval time" in text


@pytest.mark.parametrize("difficulty", ["short", "medium", "long"])
def test_difficulty_tier_present(difficulty: str):
    """All three tiers should be represented in any reasonable sample of 200.
    Failing this means the heuristic is broken or the sample is degenerate."""
    raw = yaml.safe_load(MATH_SUITE_YAML.read_text(encoding="utf-8"))
    tiers = {p["difficulty"] for p in raw["problems"]}
    assert difficulty in tiers, f"missing {difficulty} tier; tiers={tiers}"
