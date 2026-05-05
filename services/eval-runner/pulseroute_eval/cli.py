"""Click-based CLI entrypoint for the eval-runner."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
from pulseroute_router.providers.fake import FakeProvider

from pulseroute_eval.bench import bench_models, write_artifact
from pulseroute_eval.canary import (
    DEFAULT_ALERT_MARGIN,
    DEFAULT_ALERT_MIN_WINDOW,
    HttpClickHouseClient,
    InMemoryClickHouseClient,
    StubLLMJudge,
    build_alert_payload,
    post_to_slack,
    run_canary,
    should_alert,
    synthetic_request_log,
    write_run_artifact,
)
from pulseroute_eval.runner import deterministic_fake_outputs, run_suite, smoke
from pulseroute_eval.suites import GOLDEN_SUITE


@click.group()
def main() -> None:
    """PulseRoute eval CLI."""


@main.command()
@click.option(
    "--suite", default="golden", show_default=True, help="Suite name (only 'golden' for now)."
)
@click.option(
    "--provider", default="fake", show_default=True, help="Provider: fake | openai | anthropic."
)
@click.option("--model", default="fake-large", show_default=True)
@click.option("--concurrency", default=8, show_default=True)
@click.option("--json-out", "json_out", is_flag=True, help="Print JSON instead of human output.")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional path to write the JSON result artifact to.",
)
def run(
    suite: str,
    provider: str,
    model: str,
    concurrency: int,
    json_out: bool,
    output: Path | None,
) -> None:
    """Run an eval suite against a provider."""
    if suite != "golden":
        click.echo(f"unknown suite: {suite}", err=True)
        sys.exit(2)
    if provider != "fake":
        click.echo("only --provider fake is wired in this scaffold", err=True)
        sys.exit(2)

    p = FakeProvider()
    result = asyncio.run(
        run_suite(
            p,
            model=model,
            tasks=GOLDEN_SUITE,
            concurrency=concurrency,
            faked_outputs=deterministic_fake_outputs(),
        )
    )
    payload = {
        "model": result.model,
        "accuracy": result.accuracy,
        "by_category": result.by_category,
        "p95_latency_ms": result.p95_latency_ms,
        "n_tasks": len(result.tasks),
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n")

    if json_out:
        click.echo(json.dumps(payload))
        return

    click.echo(f"suite=golden model={result.model} n={len(result.tasks)}")
    click.echo(f"accuracy: {result.accuracy:.2%}")
    click.echo(f"p95 latency: {result.p95_latency_ms} ms")
    for cat, sc in sorted(result.by_category.items()):
        click.echo(f"  {cat}: {sc:.2%}")


@main.command()
@click.option(
    "--models",
    default="fake-small,fake-large",
    show_default=True,
    help="Comma-separated model names to compare.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Path to write the multi-model JSON artifact.",
)
def bench(models: str, output: Path) -> None:
    """Run the suite against multiple models and emit a Pareto-style artifact."""
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    if not model_list:
        click.echo("no models given", err=True)
        sys.exit(2)
    payload = asyncio.run(bench_models(model_list))
    write_artifact(payload, output)
    click.echo(f"wrote {output}  ({len(model_list)} models, {payload['n_tasks']} tasks each)")


@main.command()
def smoke_cmd() -> None:
    """Single-shot in-process smoke (used by CI)."""
    result = asyncio.run(smoke())
    click.echo(
        json.dumps(
            {
                "model": result.model,
                "accuracy": result.accuracy,
                "by_category": result.by_category,
                "p95_latency_ms": result.p95_latency_ms,
            },
            indent=2,
        )
    )
    if result.accuracy < 0.99:
        sys.exit(1)


# Click registers under the original function name; alias so `pulseroute-eval smoke` works.
main.add_command(smoke_cmd, name="smoke")


@main.group()
def canary() -> None:
    """Real-traffic canary commands."""


@canary.command("run")
@click.option("--canary-model", required=True, help="Treatment-arm model name.")
@click.option("--sample-rate", default=0.01, show_default=True, type=float)
@click.option("--window-hours", default=168, show_default=True, type=int)
@click.option("--seed", default=0, show_default=True, type=int)
@click.option("--tenant", "tenant_id", default=None, help="Filter to a single tenant id.")
@click.option(
    "--source",
    type=click.Choice(["clickhouse", "synthetic"]),
    default="synthetic",
    show_default=True,
    help="`clickhouse` queries the live request_log; `synthetic` runs a hermetic smoke "
    "against a Sprint-0-style 50k-row in-memory request_log.",
)
@click.option(
    "--alert-margin",
    default=DEFAULT_ALERT_MARGIN,
    show_default=True,
    type=float,
    help="Loss-margin (0.02 = 2pp) above which an alert fires.",
)
@click.option(
    "--alert-min-window",
    default=DEFAULT_ALERT_MIN_WINDOW,
    show_default=True,
    type=int,
    help="Minimum n_judged before an alert can fire.",
)
@click.option(
    "--judge-bias-against",
    default=None,
    help="Bias the stub judge against responses tagged with this string (testing).",
)
@click.option(
    "--judge-bias-strength",
    default=0.0,
    show_default=True,
    type=float,
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Where to write the run summary JSON. Defaults to eval/canary/<ts>.json.",
)
def canary_run(
    canary_model: str,
    sample_rate: float,
    window_hours: int,
    seed: int,
    tenant_id: str | None,
    source: str,
    alert_margin: float,
    alert_min_window: int,
    judge_bias_against: str | None,
    judge_bias_strength: float,
    output: Path | None,
) -> None:
    """Run a canary cycle and persist the result."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=window_hours)

    if source == "clickhouse":
        client = HttpClickHouseClient()
    else:
        rows = synthetic_request_log(n_rows=50_000, window_start=start, window_end=end, seed=42)
        client = InMemoryClickHouseClient(request_log=rows)

    judge = StubLLMJudge(bias_against=judge_bias_against, bias_strength=judge_bias_strength)

    summary = asyncio.run(
        run_canary(
            canary_model=canary_model,
            window_start=start,
            window_end=end,
            sample_rate=sample_rate,
            seed=seed,
            tenant_id=tenant_id,
            clickhouse=client,
            judge=judge,
        )
    )

    if output is None:
        ts = end.strftime("%Y%m%dT%H%M%SZ")
        output = Path("eval") / "canary" / f"{ts}.json"
    write_run_artifact(summary, output)

    payload = summary.to_dict()
    payload["alert_fired"] = should_alert(
        summary, alert_margin=alert_margin, min_window=alert_min_window
    )
    if payload["alert_fired"]:
        post_to_slack(build_alert_payload(summary))
    click.echo(json.dumps({k: payload[k] for k in payload if k != "judgments"}, indent=2))


if __name__ == "__main__":
    main()
