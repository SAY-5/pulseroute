"""Tests for the GSM8K sampler helper.

Re-running the sampler must produce byte-identical output, otherwise
``eval/suites/golden_v1/math.yaml`` would churn on every regen and the eval
baseline would lose its ``git diff`` audit trail.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "sample_gsm8k.py"
COMMITTED_YAML = REPO_ROOT / "eval" / "suites" / "golden_v1" / "math.yaml"


def _load_module():
    spec = importlib.util.spec_from_file_location("sample_gsm8k", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sample_gsm8k"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sample_gsm8k_mod():
    return _load_module()


def test_extract_answer_strips_chain_of_thought(sample_gsm8k_mod):
    raw = "It takes 2/2=<<2/2=1>>1 bolt of white fiber\nSo total is 3 bolts\n#### 3"
    assert sample_gsm8k_mod._extract_answer(raw) == "3"


def test_extract_answer_handles_multidigit(sample_gsm8k_mod):
    raw = "long chain of thought\n#### 70000"
    assert sample_gsm8k_mod._extract_answer(raw) == "70000"


def test_difficulty_tiers(sample_gsm8k_mod):
    short = "Two plus two."
    medium = "word " * 60
    long_ = "word " * 150
    assert sample_gsm8k_mod._difficulty(short) == "short"
    assert sample_gsm8k_mod._difficulty(medium) == "medium"
    assert sample_gsm8k_mod._difficulty(long_) == "long"


def test_render_yaml_is_deterministic(sample_gsm8k_mod):
    rec = {"question": "Q?", "answer": "thinking\n#### 7"}
    a = sample_gsm8k_mod.render_yaml([(0, rec)])
    b = sample_gsm8k_mod.render_yaml([(0, rec)])
    assert a == b
    assert "gsm8k_test_0" in a
    assert 'answer: "7"' in a


def test_yaml_escape_double_quotes_and_backslashes(sample_gsm8k_mod):
    out = sample_gsm8k_mod._yaml_escape('She said "hi" \\ there')
    # Must be a valid double-quoted YAML scalar with both chars escaped.
    assert out.startswith('"') and out.endswith('"')
    assert '\\"' in out
    assert "\\\\" in out


def test_committed_yaml_matches_a_fresh_render(tmp_path, sample_gsm8k_mod):
    """If someone hand-edits math.yaml, the next sampler run silently
    overwrites their changes — we want that, but the committed file must
    therefore always equal a fresh re-render."""
    if not COMMITTED_YAML.exists():
        pytest.skip("math.yaml not present (initial bootstrap)")

    out = tmp_path / "math.yaml"
    rc = sample_gsm8k_mod.build(out)
    assert rc == 0
    fresh = out.read_text(encoding="utf-8")
    committed = COMMITTED_YAML.read_text(encoding="utf-8")
    assert fresh == committed, "committed math.yaml drifted from sampler output"
