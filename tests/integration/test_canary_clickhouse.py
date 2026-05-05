"""Integration test for the canary sampler against a real ClickHouse.

Gated on ``RUN_INTEGRATION=1`` (or ``PULSEROUTE_RUN_CANARY_INTEGRATION=1``)
so the unit-test job stays hermetic. Pointed at ``CLICKHOUSE_URL`` (default
``http://localhost:8123``) — matches the migrate-check job's service
container shape.

The test:
  1. Migrates ClickHouse from scratch (so canary_results exists).
  2. Inserts a small synthetic request_log slice.
  3. Runs the sampler end-to-end via ``HttpClickHouseClient``.
  4. Asserts canary_results gained one row per judgment.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pulseroute_eval.canary import (
    HttpClickHouseClient,
    StubLLMJudge,
    run_canary,
    synthetic_request_log,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _enabled() -> bool:
    return bool(os.getenv("RUN_INTEGRATION") or os.getenv("PULSEROUTE_RUN_CANARY_INTEGRATION"))


pytestmark = pytest.mark.skipif(
    not _enabled(),
    reason="set RUN_INTEGRATION=1 to run; needs a live ClickHouse on $CLICKHOUSE_URL",
)


def _ch_post(url: str, query: str, body: bytes | None = None) -> bytes:
    full = f"{url}/?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(full, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        if resp.status >= 400:
            raise RuntimeError(f"clickhouse error {resp.status}: {resp.read()!r}")
        return resp.read()  # type: ignore[no-any-return]


def _ch_get(url: str, query: str) -> str:
    full = f"{url}/?{urllib.parse.urlencode({'query': query})}"
    with urllib.request.urlopen(full, timeout=30) as resp:  # noqa: S310
        return resp.read().decode().strip()  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def clickhouse_url() -> str:
    return os.getenv("CLICKHOUSE_URL", "http://localhost:8123")


@pytest.fixture(scope="module")
def migrated(clickhouse_url: str) -> str:
    """Apply schema migrations once per module."""
    rc = subprocess.call(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "clickhouse_migrate.py")],
        env={**os.environ, "CLICKHOUSE_URL": clickhouse_url},
    )
    assert rc == 0, "clickhouse_migrate.py failed"
    return clickhouse_url


def _seed_request_log_rows(url: str, rows: int = 200) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    sampled = synthetic_request_log(n_rows=rows, window_start=start, window_end=end, seed=42)
    # Match request_log columns exactly.
    tsv: list[str] = []
    for r in sampled:
        ts = r.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        tsv.append(
            "\t".join(
                [
                    ts,
                    r.request_id,
                    r.tenant_id,
                    r.model,
                    "fake",
                    "primary",
                    "100",
                    "20",
                    "30",
                    "10",
                    "0.0001",
                    "0",
                    "",
                ]
            )
        )
    body = ("\n".join(tsv) + "\n").encode()
    _ch_post(url, "INSERT INTO pulseroute.request_log FORMAT TSV", body=body)
    return start, end


def test_canary_run_writes_to_clickhouse(migrated: str):
    url = migrated
    start, end = _seed_request_log_rows(url, rows=400)

    client = HttpClickHouseClient(base_url=url)
    judge = StubLLMJudge()
    run_id = f"itest-{uuid.uuid4().hex[:8]}"

    summary = asyncio.run(
        run_canary(
            canary_model="fake-large",
            window_start=start,
            window_end=end,
            sample_rate=0.5,
            seed=0,
            clickhouse=client,
            judge=judge,
            run_id=run_id,
        )
    )
    assert summary.n_judged > 0

    count = _ch_get(
        url,
        f"SELECT count() FROM pulseroute.canary_results WHERE run_id = '{run_id}'",
    )
    assert int(count) == summary.n_judged

    # Sanity: judgment column is one of the three known values.
    distinct = _ch_get(
        url,
        f"SELECT count(DISTINCT judgment) FROM pulseroute.canary_results "
        f"WHERE run_id = '{run_id}' AND judgment IN ('win_treatment','win_stable','tie')",
    )
    assert int(distinct) >= 1
