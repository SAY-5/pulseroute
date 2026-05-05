"""Real-traffic canary sampler.

Pulls recent successful, non-cached requests out of ClickHouse ``request_log``,
deterministically subsamples them, replays each through both a stable arm
(the model that originally served the row) and a configurable treatment arm,
asks an LLM judge to compare the two responses, and persists the verdict to
``canary_results``.

Design notes
------------

- **Hermetic by default.** The ``ClickHouseClient`` and ``LLMJudge`` are
  Protocols. Production wires the HTTP-backed ClickHouse + a real judge.
  Tests wire ``InMemoryClickHouseClient`` + ``StubLLMJudge``. No real keys
  are read, no network is touched at test time.

- **Determinism.** Sampling is a pure function of
  ``(window_start, window_end, sample_rate, seed)``. Re-sampling the same
  window twice produces the same set of rows — this matters for
  reproducibility when a judgment is investigated after the fact.

- **Per-tenant scoping.** Optional ``tenant_id`` filter; default is all
  tenants. The seed-script seed (``42``) is **not** the canary seed; the
  default canary seed is ``0`` so a Sprint-0 reseed of request_log doesn't
  accidentally invalidate canary baselines.

- **Alerting.** Slack webhook is env-gated via
  ``PULSEROUTE_CANARY_SLACK_WEBHOOK``. When unset, no HTTP call is made.
  Alert payloads are produced (and unit-tested) regardless of whether
  they're sent — so a recording mock can capture them in CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pulseroute_router.providers.fake import FakeProvider
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage

# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

JUDGMENT_WIN_TREATMENT = "win_treatment"
JUDGMENT_WIN_STABLE = "win_stable"
JUDGMENT_TIE = "tie"

# Margin — over a 1000-sample window — that triggers the alert. Matches the
# README's drift-detection promise. Configurable per call but defaulted here
# so the constant has one canonical home.
DEFAULT_ALERT_MARGIN = 0.02
DEFAULT_ALERT_MIN_WINDOW = 1000


@dataclass(frozen=True, slots=True)
class SampledRequest:
    """A row pulled from ``request_log``, projected to what the canary needs."""

    request_id: str
    tenant_id: str
    model: str  # the model that originally served this request — the stable arm
    prompt_text: str  # synthesised user prompt; production rebuilds from raw payload
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    sampled_request_id: str
    tenant_id: str
    stable_model: str
    canary_model: str
    stable_score: float
    canary_score: float
    judgment: str  # JUDGMENT_*
    judge_model: str


@dataclass
class CanaryRunSummary:
    run_id: str
    canary_model: str
    window_start: datetime
    window_end: datetime
    sample_rate: float
    seed: int
    tenant_id: str | None
    n_sampled: int
    n_judged: int
    wins: int
    losses: int
    ties: int
    judgments: list[JudgmentResult] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_judged if self.n_judged else 0.0

    @property
    def loss_rate(self) -> float:
        return self.losses / self.n_judged if self.n_judged else 0.0

    @property
    def margin(self) -> float:
        """Loss minus win rate. Positive => canary is losing."""
        return self.loss_rate - self.win_rate

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"] = self.window_end.isoformat()
        d["win_rate"] = round(self.win_rate, 4)
        d["loss_rate"] = round(self.loss_rate, 4)
        d["margin"] = round(self.margin, 4)
        return d


# ----------------------------------------------------------------------
# ClickHouse client Protocol + concrete implementations
# ----------------------------------------------------------------------


class ClickHouseClient(Protocol):
    """Minimal surface used by the canary sampler. Plays nicely with both
    HTTP-driven ClickHouse and an in-memory test fake."""

    def query_request_log(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        tenant_id: str | None,
    ) -> list[SampledRequest]: ...

    def insert_canary_results(self, rows: Iterable[dict[str, Any]]) -> None: ...


class HttpClickHouseClient:
    """HTTP-backed ClickHouse client (matches scripts/clickhouse_migrate.py)."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 30.0) -> None:
        self.base_url = base_url or os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
        self.timeout = timeout

    def _post(self, query: str, body: bytes | None = None) -> bytes:
        url = f"{self.base_url}/?{urllib.parse.urlencode({'query': query})}"
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            if resp.status >= 400:
                raise RuntimeError(f"clickhouse error {resp.status}: {resp.read()!r}")
            return resp.read()  # type: ignore[no-any-return]

    def query_request_log(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        tenant_id: str | None,
    ) -> list[SampledRequest]:
        # JSONEachRow is easier to parse than TSV; ClickHouse emits one JSON
        # object per line.
        tenant_filter = ""
        if tenant_id is not None:
            tenant_filter = f" AND tenant_id = '{_escape_sql(tenant_id)}'"
        q = (
            "SELECT request_id, tenant_id, model, "
            "concat('replay-', request_id) AS prompt_text, "
            "toString(timestamp) AS ts "
            "FROM pulseroute.request_log "
            f"WHERE timestamp >= toDateTime64('{window_start.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}', 3) "
            f"AND timestamp < toDateTime64('{window_end.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}', 3) "
            f"AND cache_hit = 0 AND error_code = ''{tenant_filter} "
            "ORDER BY request_id "
            "FORMAT JSONEachRow"
        )
        raw = self._post(q)
        out: list[SampledRequest] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            out.append(
                SampledRequest(
                    request_id=row["request_id"],
                    tenant_id=row["tenant_id"],
                    model=row["model"],
                    prompt_text=row["prompt_text"],
                    timestamp=_parse_ch_datetime(row["ts"]),
                )
            )
        return out

    def insert_canary_results(self, rows: Iterable[dict[str, Any]]) -> None:
        rows_list = list(rows)
        if not rows_list:
            return
        body = ("\n".join(json.dumps(r) for r in rows_list) + "\n").encode()
        self._post("INSERT INTO pulseroute.canary_results FORMAT JSONEachRow", body=body)


class InMemoryClickHouseClient:
    """Test double. Stores rows in lists; same surface as HttpClickHouseClient."""

    def __init__(self, request_log: list[SampledRequest] | None = None) -> None:
        self.request_log = list(request_log or [])
        self.canary_inserts: list[dict[str, Any]] = []

    def query_request_log(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        tenant_id: str | None,
    ) -> list[SampledRequest]:
        out = [
            r
            for r in self.request_log
            if window_start <= r.timestamp < window_end
            and (tenant_id is None or r.tenant_id == tenant_id)
        ]
        # Match the HTTP client's deterministic ORDER BY request_id.
        out.sort(key=lambda r: r.request_id)
        return out

    def insert_canary_results(self, rows: Iterable[dict[str, Any]]) -> None:
        self.canary_inserts.extend(rows)


def _escape_sql(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _parse_ch_datetime(s: str) -> datetime:
    # ClickHouse toString(DateTime64(3)) emits ``YYYY-MM-DD HH:MM:SS.fff``.
    return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=UTC)


# ----------------------------------------------------------------------
# LLM judge Protocol + stub
# ----------------------------------------------------------------------


class LLMJudge(Protocol):
    name: str

    async def score(self, prompt: str, response: str) -> float: ...


class StubLLMJudge:
    """Deterministic stand-in for tests.

    Hashes ``(prompt, response, model_tag)`` into a stable score in [0, 1].
    Optionally biases toward one model tag so tests can simulate a regression
    without race conditions in async test code.
    """

    name = "stub-judge-v1"

    def __init__(self, *, bias_against: str | None = None, bias_strength: float = 0.0) -> None:
        self.bias_against = bias_against
        self.bias_strength = bias_strength

    async def score(self, prompt: str, response: str) -> float:
        # Hash to [0.5, 1.0] so default scores cluster in the "passable" band.
        h = hashlib.sha256(f"{prompt}|{response}".encode()).digest()
        base = 0.5 + (h[0] / 255.0) * 0.5
        # Tag-based bias: if `response` was tagged with the canary model and
        # bias_against matches, penalise by bias_strength.
        if self.bias_against and self.bias_against in response:
            base = max(0.0, base - self.bias_strength)
        return round(base, 4)


# ----------------------------------------------------------------------
# Deterministic sampling
# ----------------------------------------------------------------------


def deterministic_sample(
    rows: list[SampledRequest],
    *,
    sample_rate: float,
    seed: int,
) -> list[SampledRequest]:
    """Pick a deterministic subset of ``rows``.

    ``rows`` is expected to be in a stable order (the HTTP client and
    in-memory client both ORDER BY request_id). For a given
    ``(rows, sample_rate, seed)`` the output is bit-identical across runs,
    Python implementations, and machines.

    We avoid ``random.sample`` because its choice depends on the size of
    the population in a way that's awkward to reproduce externally. Instead
    we score each row with a salted SHA-256 and pick rows whose score falls
    below ``sample_rate * 2**32``. This is a stateless filter: doubling the
    population doesn't reshuffle existing rows.
    """
    if not 0.0 < sample_rate <= 1.0:
        raise ValueError(f"sample_rate must be in (0, 1]; got {sample_rate}")
    if not rows:
        return []
    threshold = int(sample_rate * (1 << 32))
    salt = f"pulseroute-canary:{seed}".encode()
    selected: list[SampledRequest] = []
    for row in rows:
        digest = hashlib.sha256(salt + row.request_id.encode()).digest()
        score = int.from_bytes(digest[:4], "big")
        if score < threshold:
            selected.append(row)
    return selected


# ----------------------------------------------------------------------
# Replay + judgment
# ----------------------------------------------------------------------


# Type for a function that returns a response given a model + prompt. Lets us
# avoid wiring the gateway's full provider registry into the canary; the
# default uses FakeProvider for the hermetic case.
Replayer = Callable[[str, str], Awaitable[str]]


async def _fake_replayer(model: str, prompt: str) -> str:
    """Default replayer used when no provider registry is supplied. Drives the
    same FakeProvider that powers the eval-runner's hermetic baseline."""
    p = FakeProvider()
    resp = await p.complete(
        ChatCompletionRequest(
            model=model if model in {"fake-small", "fake-large"} else "fake-large",
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=128,
        )
    )
    # Tag the response with the model so a stub judge can apply a bias.
    return f"[{model}] {resp.content}"


def judge_one(
    *,
    sampled: SampledRequest,
    canary_model: str,
    stable_score: float,
    canary_score: float,
    judge_name: str,
    dead_zone: float = 0.05,
) -> JudgmentResult:
    """Pure scoring step — no IO. Separate so unit tests can drive it directly."""
    delta = canary_score - stable_score
    if delta > dead_zone:
        verdict = JUDGMENT_WIN_TREATMENT
    elif -delta > dead_zone:
        verdict = JUDGMENT_WIN_STABLE
    else:
        verdict = JUDGMENT_TIE
    return JudgmentResult(
        sampled_request_id=sampled.request_id,
        tenant_id=sampled.tenant_id,
        stable_model=sampled.model,
        canary_model=canary_model,
        stable_score=round(stable_score, 4),
        canary_score=round(canary_score, 4),
        judgment=verdict,
        judge_model=judge_name,
    )


# ----------------------------------------------------------------------
# Alerting
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertPayload:
    text: str
    summary: dict[str, Any]


def build_alert_payload(summary: CanaryRunSummary) -> AlertPayload:
    text = (
        f":rotating_light: canary regression — {summary.canary_model} losing to "
        f"{', '.join({j.stable_model for j in summary.judgments}) or '<mixed>'}\n"
        f"window={summary.window_start.isoformat()} → {summary.window_end.isoformat()}  "
        f"n={summary.n_judged}  wins={summary.wins} losses={summary.losses} ties={summary.ties}  "
        f"margin={summary.margin:+.2%}"
    )
    return AlertPayload(text=text, summary=summary.to_dict())


def should_alert(
    summary: CanaryRunSummary,
    *,
    alert_margin: float = DEFAULT_ALERT_MARGIN,
    min_window: int = DEFAULT_ALERT_MIN_WINDOW,
) -> bool:
    """Alert iff the canary is losing by > ``alert_margin`` over a window of
    at least ``min_window`` judgments. Wins and ties never alert; small
    windows never alert (statistical-power floor)."""
    if summary.n_judged < min_window:
        return False
    return summary.margin > alert_margin


def post_to_slack(
    payload: AlertPayload,
    *,
    webhook_url: str | None = None,
    poster: Callable[[str, bytes], Any] | None = None,
) -> bool:
    """POST the payload to Slack iff webhook is configured.

    Returns True if a post was attempted, False if the webhook is unset
    (i.e. tests / disabled-by-default behaviour). ``poster`` is injectable
    so tests don't actually hit the network."""
    url = webhook_url or os.getenv("PULSEROUTE_CANARY_SLACK_WEBHOOK")
    if not url:
        return False
    body = json.dumps({"text": payload.text}).encode()
    if poster is not None:
        poster(url, body)
        return True
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        if resp.status >= 400:
            raise RuntimeError(f"slack webhook error {resp.status}")
    return True


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


async def run_canary(
    *,
    canary_model: str,
    window_start: datetime,
    window_end: datetime,
    sample_rate: float = 0.01,
    seed: int = 0,
    tenant_id: str | None = None,
    clickhouse: ClickHouseClient,
    judge: LLMJudge,
    replayer: Replayer | None = None,
    run_id: str | None = None,
) -> CanaryRunSummary:
    """End-to-end canary: pull window → subsample → replay both arms →
    judge → persist → return summary. No IO beyond the injected
    ``clickhouse`` and ``replayer``."""
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    replayer_fn = replayer or _fake_replayer
    rid = run_id or f"canary-{uuid.uuid4().hex[:12]}"

    rows = clickhouse.query_request_log(
        window_start=window_start, window_end=window_end, tenant_id=tenant_id
    )
    sampled = deterministic_sample(rows, sample_rate=sample_rate, seed=seed)

    judgments: list[JudgmentResult] = []
    insert_rows: list[dict[str, Any]] = []
    judged_at = datetime.now(UTC)

    for row in sampled:
        stable_resp, canary_resp = await asyncio.gather(
            replayer_fn(row.model, row.prompt_text),
            replayer_fn(canary_model, row.prompt_text),
        )
        stable_score = await judge.score(row.prompt_text, stable_resp)
        canary_score = await judge.score(row.prompt_text, canary_resp)
        verdict = judge_one(
            sampled=row,
            canary_model=canary_model,
            stable_score=stable_score,
            canary_score=canary_score,
            judge_name=judge.name,
        )
        judgments.append(verdict)
        insert_rows.append(
            {
                "judged_at": judged_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "run_id": rid,
                "sampled_request_id": verdict.sampled_request_id,
                "tenant_id": verdict.tenant_id,
                "stable_model": verdict.stable_model,
                "canary_model": verdict.canary_model,
                "stable_score": verdict.stable_score,
                "canary_score": verdict.canary_score,
                "judgment": verdict.judgment,
                "judge_model": verdict.judge_model,
                "window_start": window_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "window_end": window_end.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "sample_rate": sample_rate,
                "seed": seed,
            }
        )

    clickhouse.insert_canary_results(insert_rows)

    wins = sum(1 for j in judgments if j.judgment == JUDGMENT_WIN_TREATMENT)
    losses = sum(1 for j in judgments if j.judgment == JUDGMENT_WIN_STABLE)
    ties = sum(1 for j in judgments if j.judgment == JUDGMENT_TIE)

    return CanaryRunSummary(
        run_id=rid,
        canary_model=canary_model,
        window_start=window_start,
        window_end=window_end,
        sample_rate=sample_rate,
        seed=seed,
        tenant_id=tenant_id,
        n_sampled=len(sampled),
        n_judged=len(judgments),
        wins=wins,
        losses=losses,
        ties=ties,
        judgments=judgments,
    )


# ----------------------------------------------------------------------
# Synthetic seed-style request_log generator (for hermetic CLI smoke + tests)
# ----------------------------------------------------------------------


def synthetic_request_log(
    *,
    n_rows: int,
    window_start: datetime,
    window_end: datetime,
    seed: int = 42,
    tenants: list[str] | None = None,
    models: list[str] | None = None,
) -> list[SampledRequest]:
    """Mirror of ``scripts/seed.py``'s request_log distribution, projected to
    the columns the canary needs. Used by tests and the CLI's hermetic
    smoke path so we don't need a running ClickHouse to demonstrate the
    end-to-end flow.
    """
    tenants = tenants or ["tenant_costcap", "tenant_quality", "tenant_latency"]
    models = models or ["fake-small", "fake-large"]
    rng = random.Random(seed)
    span = (window_end - window_start).total_seconds()
    out: list[SampledRequest] = []
    for i in range(n_rows):
        ts = window_start + timedelta(seconds=rng.uniform(0, span))
        out.append(
            SampledRequest(
                request_id=f"req_{int(ts.timestamp())}_{i:06d}",
                tenant_id=rng.choice(tenants),
                model=rng.choice(models),
                prompt_text=f"synthetic prompt {i}: {rng.randint(0, 1000000)}",
                timestamp=ts,
            )
        )
    return out


# ----------------------------------------------------------------------
# Output artifact
# ----------------------------------------------------------------------


def write_run_artifact(summary: CanaryRunSummary, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = summary.to_dict()
    # Strip the per-judgment list to keep run artifacts compact; the
    # full per-judgment record lives in ClickHouse.
    payload.pop("judgments", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
