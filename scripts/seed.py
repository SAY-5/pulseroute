"""Seed Postgres + ClickHouse with demo data.

Three tenants with distinct policies, and 50k synthetic ``request_log`` rows
spanning the last 7 days with realistic distributions."""

from __future__ import annotations

import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

import psycopg2

PG_URL = os.getenv(
    "PULSEROUTE_POSTGRES_URL_SYNC",
    "postgresql://pulseroute:pulseroute@localhost:5432/pulseroute",
)
CH_URL = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
N_ROWS = int(os.getenv("SEED_ROWS", "50000"))


def _ch(query: str, body: bytes | None = None) -> None:
    url = f"{CH_URL}/?{urllib.parse.urlencode({'query': query})}"
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        if resp.status >= 400:
            raise RuntimeError(f"clickhouse error {resp.status}: {resp.read()!r}")


def seed_postgres() -> None:
    conn = psycopg2.connect(PG_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        tenants = [
            ("tenant_costcap", "Cost-Capped Demo"),
            ("tenant_quality", "Quality-First Demo"),
            ("tenant_latency", "Latency-First Demo"),
        ]
        for tid, name in tenants:
            cur.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (tid, name),
            )
        policies = [
            (
                "p_costcap",
                "tenant_costcap",
                {
                    "routing_strategy": "cost_capped",
                    "cost_ceiling_usd_per_day": 5.0,
                    "allowed_models": ["gpt-4o-mini", "claude-3-haiku"],
                    "pii_redaction": True,
                },
            ),
            (
                "p_quality",
                "tenant_quality",
                {
                    "routing_strategy": "quality_first",
                    "cost_ceiling_usd_per_day": 0.0,
                    "allowed_models": ["gpt-4o", "claude-3-5-sonnet"],
                    "pii_redaction": False,
                },
            ),
            (
                "p_latency",
                "tenant_latency",
                {
                    "routing_strategy": "latency_first",
                    "cost_ceiling_usd_per_day": 0.0,
                    "allowed_models": ["gpt-4o-mini", "claude-3-haiku"],
                    "pii_redaction": False,
                },
            ),
        ]
        import json

        for pid, tid, cfg in policies:
            cur.execute(
                "INSERT INTO policies (id, tenant_id, config) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (pid, tid, json.dumps(cfg)),
            )
    conn.close()
    print("seeded postgres")


def seed_clickhouse() -> None:
    rng = random.Random(42)
    tenants = ["tenant_costcap", "tenant_quality", "tenant_latency"]
    models = ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet", "claude-3-haiku"]
    providers = {
        "gpt-4o": "openai",
        "gpt-4o-mini": "openai",
        "claude-3-5-sonnet": "anthropic",
        "claude-3-haiku": "anthropic",
    }
    reasons = ["primary", "failover", "all_breakers_open"]
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    span_seconds = (end - start).total_seconds()

    rows: list[str] = []
    for i in range(N_ROWS):
        ts = start + timedelta(seconds=rng.uniform(0, span_seconds))
        tenant = rng.choice(tenants)
        model = rng.choice(models)
        provider = providers[model]
        latency = int(rng.gauss(450, 220))
        latency = max(50, min(5000, latency))
        ttft = int(latency * rng.uniform(0.15, 0.45))
        tokens_in = rng.randint(50, 2000)
        tokens_out = rng.randint(20, 800)
        cost = tokens_in * 0.00001 + tokens_out * 0.00003
        cache_hit = 1 if rng.random() < 0.32 else 0
        error = "" if rng.random() > 0.02 else rng.choice(["upstream_5xx", "timeout"])
        reason = rng.choice(reasons)
        rid = f"req_{int(ts.timestamp())}_{i:06d}"
        rows.append(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\t{rid}\t{tenant}\t{model}\t{provider}\t{reason}\t{latency}\t{ttft}\t{tokens_in}\t{tokens_out}\t{cost:.6f}\t{cache_hit}\t{error}"
        )

    body = ("\n".join(rows) + "\n").encode()
    _ch("INSERT INTO pulseroute.request_log FORMAT TSV", body=body)
    print(f"seeded clickhouse with {N_ROWS} rows")


def main() -> int:
    try:
        seed_postgres()
    except Exception as exc:
        print(f"postgres seed failed: {exc}", file=sys.stderr)
        return 1
    try:
        seed_clickhouse()
    except Exception as exc:
        print(f"clickhouse seed failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
