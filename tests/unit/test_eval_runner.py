"""Eval runner smoke + scoring tests."""

from __future__ import annotations

import pytest
from pulseroute_eval.runner import deterministic_fake_outputs, run_suite, smoke
from pulseroute_eval.scoring import score
from pulseroute_eval.suites import GOLDEN_SUITE, EvalTask
from pulseroute_router.providers.fake import FakeProvider


def test_score_correct_answer():
    t = EvalTask(id="m", category="math", prompt="2+2?", expected="4")
    assert score(t, "the answer is 4") == 1.0


def test_score_wrong_answer():
    t = EvalTask(id="m", category="math", prompt="2+2?", expected="4")
    assert score(t, "the answer is 5") == 0.0


def test_score_refusal_recognised():
    t = EvalTask(id="r", category="refusal", prompt="bad", expected=None, refusal_expected=True)
    assert score(t, "I cannot help with that") == 1.0


def test_score_refusal_failed():
    t = EvalTask(id="r", category="refusal", prompt="bad", expected=None, refusal_expected=True)
    assert score(t, "Sure, here's how...") == 0.0


@pytest.mark.asyncio
async def test_smoke_run_completes():
    result = await smoke()
    assert result.model == "fake-large"
    assert len(result.tasks) == len(GOLDEN_SUITE)
    # With deterministic crafted outputs the smoke should achieve full marks.
    assert result.accuracy == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_run_suite_against_fake_provider_without_crafted_outputs():
    # Without crafted outputs, FakeProvider returns deterministic gibberish that
    # scores zero — but the pipeline must still run end-to-end without crashing.
    result = await run_suite(FakeProvider(), model="fake-large", tasks=GOLDEN_SUITE[:5])
    assert len(result.tasks) == 5


@pytest.mark.asyncio
async def test_concurrency_cap_respected():
    # Smoke test: the semaphore-bounded gather should still complete with high task count.
    result = await run_suite(
        FakeProvider(),
        model="fake-large",
        tasks=GOLDEN_SUITE,
        concurrency=4,
        faked_outputs=deterministic_fake_outputs(),
    )
    assert len(result.tasks) == len(GOLDEN_SUITE)
