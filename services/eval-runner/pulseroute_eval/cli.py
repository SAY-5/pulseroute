"""Click-based CLI entrypoint for the eval-runner."""

from __future__ import annotations

import asyncio
import json
import sys

import click
from pulseroute_router.providers.fake import FakeProvider

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
def run(suite: str, provider: str, model: str, concurrency: int, json_out: bool) -> None:
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
    if json_out:
        click.echo(
            json.dumps(
                {
                    "model": result.model,
                    "accuracy": result.accuracy,
                    "by_category": result.by_category,
                    "p95_latency_ms": result.p95_latency_ms,
                    "n_tasks": len(result.tasks),
                }
            )
        )
        return

    click.echo(f"suite=golden model={result.model} n={len(result.tasks)}")
    click.echo(f"accuracy: {result.accuracy:.2%}")
    click.echo(f"p95 latency: {result.p95_latency_ms} ms")
    for cat, sc in sorted(result.by_category.items()):
        click.echo(f"  {cat}: {sc:.2%}")


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


if __name__ == "__main__":
    main()
