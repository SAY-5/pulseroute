"""Hermetic unit tests for the canary sampler.

These tests use ``InMemoryClickHouseClient`` + ``StubLLMJudge`` so CI doesn't
need testcontainers, real ClickHouse, or any LLM keys. The integration test
that exercises real ClickHouse lives in tests/integration/ behind
``RUN_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from pulseroute_eval.canary import (
    DEFAULT_ALERT_MARGIN,
    DEFAULT_ALERT_MIN_WINDOW,
    JUDGMENT_TIE,
    JUDGMENT_WIN_STABLE,
    JUDGMENT_WIN_TREATMENT,
    AlertPayload,
    CanaryRunSummary,
    InMemoryClickHouseClient,
    JudgmentResult,
    SampledRequest,
    StubLLMJudge,
    build_alert_payload,
    deterministic_sample,
    judge_one,
    post_to_slack,
    run_canary,
    should_alert,
    synthetic_request_log,
    write_run_artifact,
)

# ----------------------------------------------------------------------
# Sampler determinism
# ----------------------------------------------------------------------


def _seed_request_log() -> list[SampledRequest]:
    base = datetime(2026, 4, 1, tzinfo=UTC)
    return [
        SampledRequest(
            request_id=f"req_{i:06d}",
            tenant_id=("tenant_a" if i % 2 == 0 else "tenant_b"),
            model="fake-small" if i % 3 == 0 else "fake-large",
            prompt_text=f"prompt {i}",
            timestamp=base + timedelta(seconds=i),
        )
        for i in range(2000)
    ]


def test_deterministic_sample_is_reproducible():
    rows = _seed_request_log()
    a = deterministic_sample(rows, sample_rate=0.05, seed=42)
    b = deterministic_sample(rows, sample_rate=0.05, seed=42)
    assert [r.request_id for r in a] == [r.request_id for r in b]
    # Sample size is bounded but not guaranteed exact (it's a stateless
    # hash filter); within 30% of the expected value over 2000 rows is
    # a comfortable window.
    expected = int(len(rows) * 0.05)
    assert 0.7 * expected <= len(a) <= 1.3 * expected, len(a)


def test_deterministic_sample_changes_with_seed():
    rows = _seed_request_log()
    a = deterministic_sample(rows, sample_rate=0.05, seed=1)
    b = deterministic_sample(rows, sample_rate=0.05, seed=2)
    assert {r.request_id for r in a} != {r.request_id for r in b}


def test_deterministic_sample_is_stateless_under_population_growth():
    """If new rows are appended, the existing rows' selection status stays
    fixed. This is the property that lets investigators re-sample a window
    after the fact."""
    rows = _seed_request_log()
    selected_initial = {r.request_id for r in deterministic_sample(rows, sample_rate=0.05, seed=7)}
    # Append more rows and re-sample.
    extra = [
        SampledRequest(
            request_id=f"req_{i:06d}",
            tenant_id="tenant_a",
            model="fake-large",
            prompt_text="x",
            timestamp=datetime(2026, 4, 2, tzinfo=UTC) + timedelta(seconds=i),
        )
        for i in range(2000, 3000)
    ]
    selected_after = {
        r.request_id for r in deterministic_sample(rows + extra, sample_rate=0.05, seed=7)
    }
    # Every originally-selected row must still be selected.
    assert selected_initial.issubset(selected_after)


def test_deterministic_sample_rejects_invalid_rate():
    with pytest.raises(ValueError):
        deterministic_sample(_seed_request_log(), sample_rate=0.0, seed=0)
    with pytest.raises(ValueError):
        deterministic_sample(_seed_request_log(), sample_rate=1.5, seed=0)


def test_deterministic_sample_handles_empty_input():
    assert deterministic_sample([], sample_rate=0.1, seed=0) == []


def test_in_memory_client_filters_by_tenant():
    rows = _seed_request_log()
    client = InMemoryClickHouseClient(request_log=rows)
    base = datetime(2026, 4, 1, tzinfo=UTC)
    out = client.query_request_log(
        window_start=base,
        window_end=base + timedelta(hours=1),
        tenant_id="tenant_a",
    )
    assert all(r.tenant_id == "tenant_a" for r in out)
    assert len(out) > 0


def test_in_memory_client_window_excludes_end():
    """ClickHouse window is half-open `[start, end)`; the in-memory client
    must match for tests to be predictive."""
    rows = _seed_request_log()
    client = InMemoryClickHouseClient(request_log=rows)
    base = datetime(2026, 4, 1, tzinfo=UTC)
    edge = rows[100].timestamp
    excl = client.query_request_log(window_start=base, window_end=edge, tenant_id=None)
    assert rows[100].request_id not in {r.request_id for r in excl}


# ----------------------------------------------------------------------
# Stub judge
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_judge_is_deterministic():
    j = StubLLMJudge()
    a = await j.score("p", "r")
    b = await j.score("p", "r")
    assert a == b
    assert 0.5 <= a <= 1.0


@pytest.mark.asyncio
async def test_stub_judge_bias_lowers_score():
    j = StubLLMJudge(bias_against="canary-tag", bias_strength=0.4)
    unbiased = await j.score("p", "[stable] response")
    biased = await j.score("p", "[canary-tag] response")
    assert biased <= unbiased


# ----------------------------------------------------------------------
# Pure judging (no IO)
# ----------------------------------------------------------------------


def _row(rid: str = "req_x") -> SampledRequest:
    return SampledRequest(
        request_id=rid,
        tenant_id="tenant_a",
        model="fake-small",
        prompt_text="p",
        timestamp=datetime(2026, 4, 1, tzinfo=UTC),
    )


def test_judge_one_dead_zone_yields_tie():
    out = judge_one(
        sampled=_row(),
        canary_model="fake-large",
        stable_score=0.7,
        canary_score=0.72,
        judge_name="stub",
    )
    assert out.judgment == JUDGMENT_TIE


def test_judge_one_canary_wins():
    out = judge_one(
        sampled=_row(),
        canary_model="fake-large",
        stable_score=0.6,
        canary_score=0.9,
        judge_name="stub",
    )
    assert out.judgment == JUDGMENT_WIN_TREATMENT


def test_judge_one_canary_loses():
    out = judge_one(
        sampled=_row(),
        canary_model="fake-large",
        stable_score=0.9,
        canary_score=0.6,
        judge_name="stub",
    )
    assert out.judgment == JUDGMENT_WIN_STABLE


# ----------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------


def _summary(*, n: int, wins: int, losses: int, ties: int) -> CanaryRunSummary:
    judgments = (
        [
            JudgmentResult(
                sampled_request_id=f"r{i}",
                tenant_id="tenant_a",
                stable_model="fake-small",
                canary_model="fake-large",
                stable_score=0.5,
                canary_score=0.5,
                judgment=JUDGMENT_TIE,
                judge_model="stub",
            )
            for i in range(min(n, 3))
        ]
        if n
        else []
    )
    return CanaryRunSummary(
        run_id="r",
        canary_model="fake-large",
        window_start=datetime(2026, 4, 1, tzinfo=UTC),
        window_end=datetime(2026, 4, 8, tzinfo=UTC),
        sample_rate=0.01,
        seed=0,
        tenant_id=None,
        n_sampled=n,
        n_judged=n,
        wins=wins,
        losses=losses,
        ties=ties,
        judgments=judgments,
    )


def test_alert_fires_when_canary_loses_by_more_than_threshold():
    s = _summary(n=DEFAULT_ALERT_MIN_WINDOW, wins=400, losses=440, ties=160)
    assert s.margin > DEFAULT_ALERT_MARGIN
    assert should_alert(s) is True


def test_alert_does_not_fire_on_tie_or_win():
    win = _summary(n=DEFAULT_ALERT_MIN_WINDOW, wins=440, losses=400, ties=160)
    tie = _summary(n=DEFAULT_ALERT_MIN_WINDOW, wins=400, losses=400, ties=200)
    assert should_alert(win) is False
    assert should_alert(tie) is False


def test_alert_does_not_fire_below_min_window():
    """Even a 5pp loss-margin must not alert if the sample is below the
    statistical-power floor."""
    s = _summary(n=200, wins=80, losses=100, ties=20)
    assert s.margin > DEFAULT_ALERT_MARGIN
    assert should_alert(s) is False


def test_alert_threshold_is_configurable():
    s = _summary(n=DEFAULT_ALERT_MIN_WINDOW, wins=480, losses=510, ties=10)
    # Default threshold is 2pp — at 3pp margin this fires.
    assert should_alert(s, alert_margin=0.02) is True
    # Tighten the threshold and it still fires.
    assert should_alert(s, alert_margin=0.005) is True
    # Loosen past the actual margin and it stops firing.
    assert should_alert(s, alert_margin=0.10) is False


def test_post_to_slack_no_webhook_returns_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PULSEROUTE_CANARY_SLACK_WEBHOOK", raising=False)
    payload = AlertPayload(text="t", summary={})
    assert post_to_slack(payload) is False


def test_post_to_slack_uses_recording_poster():
    """Recording mock — never actually hits the network."""
    captured: list[tuple[str, bytes]] = []

    def poster(url: str, body: bytes) -> None:
        captured.append((url, body))

    payload = AlertPayload(text="alert!", summary={"x": 1})
    posted = post_to_slack(payload, webhook_url="https://example.invalid/hook", poster=poster)
    assert posted is True
    assert len(captured) == 1
    assert captured[0][0] == "https://example.invalid/hook"
    assert b"alert!" in captured[0][1]


def test_build_alert_payload_includes_margin():
    s = _summary(n=DEFAULT_ALERT_MIN_WINDOW, wins=400, losses=440, ties=160)
    payload = build_alert_payload(s)
    assert "margin=" in payload.text
    assert payload.summary["n_judged"] == DEFAULT_ALERT_MIN_WINDOW


# ----------------------------------------------------------------------
# End-to-end orchestrator
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_canary_end_to_end_with_in_memory_client(tmp_path: pathlib.Path):
    base = datetime(2026, 4, 1, tzinfo=UTC)
    rows = synthetic_request_log(
        n_rows=5000, window_start=base, window_end=base + timedelta(days=7), seed=42
    )
    client = InMemoryClickHouseClient(request_log=rows)
    judge = StubLLMJudge()

    summary = await run_canary(
        canary_model="fake-large",
        window_start=base,
        window_end=base + timedelta(days=7),
        sample_rate=0.05,
        seed=0,
        clickhouse=client,
        judge=judge,
    )

    assert summary.n_judged == summary.n_sampled
    assert summary.n_judged > 0
    assert summary.wins + summary.losses + summary.ties == summary.n_judged
    # Insertions must equal judgments.
    assert len(client.canary_inserts) == summary.n_judged
    # All inserted rows carry the run_id, both window bounds, the seed, and
    # the sample rate — these are what make a stored result auditable.
    for ins in client.canary_inserts:
        assert ins["run_id"] == summary.run_id
        assert ins["seed"] == 0
        assert ins["sample_rate"] == 0.05

    # Round-trip the artifact.
    out = tmp_path / "canary.json"
    write_run_artifact(summary, out)
    reloaded = json.loads(out.read_text())
    assert reloaded["run_id"] == summary.run_id
    assert "judgments" not in reloaded  # heavy field must be stripped


@pytest.mark.asyncio
async def test_run_canary_alerts_when_judge_biased_against_canary():
    base = datetime(2026, 4, 1, tzinfo=UTC)
    rows = synthetic_request_log(
        n_rows=20_000, window_start=base, window_end=base + timedelta(days=7), seed=42
    )
    client = InMemoryClickHouseClient(request_log=rows)
    # Strong bias: every canary response gets penalised by 0.4 — well past
    # the 0.05 dead-zone — so judge_one should mark almost every judgment
    # as win_stable.
    judge = StubLLMJudge(bias_against="fake-large", bias_strength=0.4)

    summary = await run_canary(
        canary_model="fake-large",
        window_start=base,
        window_end=base + timedelta(days=7),
        sample_rate=0.10,
        seed=0,
        clickhouse=client,
        judge=judge,
    )
    assert summary.n_judged >= DEFAULT_ALERT_MIN_WINDOW
    assert summary.losses > summary.wins
    assert should_alert(summary) is True
    assert build_alert_payload(summary).text.startswith(":rotating_light:")


@pytest.mark.asyncio
async def test_run_canary_per_tenant_filter_works():
    base = datetime(2026, 4, 1, tzinfo=UTC)
    rows = synthetic_request_log(
        n_rows=3000, window_start=base, window_end=base + timedelta(days=7), seed=42
    )
    client = InMemoryClickHouseClient(request_log=rows)
    judge = StubLLMJudge()

    scoped = await run_canary(
        canary_model="fake-large",
        window_start=base,
        window_end=base + timedelta(days=7),
        sample_rate=0.5,
        seed=0,
        tenant_id="tenant_quality",
        clickhouse=client,
        judge=judge,
    )
    # Every persisted row must be from the requested tenant.
    assert {ins["tenant_id"] for ins in client.canary_inserts} == {"tenant_quality"}
    # And the unscoped run yields strictly more sampled rows.
    client2 = InMemoryClickHouseClient(request_log=rows)
    unscoped = await run_canary(
        canary_model="fake-large",
        window_start=base,
        window_end=base + timedelta(days=7),
        sample_rate=0.5,
        seed=0,
        tenant_id=None,
        clickhouse=client2,
        judge=judge,
    )
    assert unscoped.n_sampled > scoped.n_sampled


@pytest.mark.asyncio
async def test_run_canary_rejects_inverted_window():
    base = datetime(2026, 4, 1, tzinfo=UTC)
    client = InMemoryClickHouseClient(request_log=[])
    judge = StubLLMJudge()
    with pytest.raises(ValueError):
        await run_canary(
            canary_model="fake-large",
            window_start=base,
            window_end=base - timedelta(hours=1),
            sample_rate=0.01,
            seed=0,
            clickhouse=client,
            judge=judge,
        )
