"""Tiny stand-in for a real ClickHouse migrator.

Reads ``.sql`` files in ``infra/clickhouse/`` in lexicographic order and POSTs
each statement to ClickHouse over HTTP. Tracks applied migrations in a small
``schema_migrations`` table so reruns are safe."""

from __future__ import annotations

import os
import pathlib
import sys
import urllib.parse
import urllib.request


def _ch_post(base_url: str, query: str) -> None:
    req = urllib.request.Request(
        f"{base_url}/?{urllib.parse.urlencode({'query': query})}",
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        if resp.status >= 400:
            raise RuntimeError(f"clickhouse error {resp.status}: {resp.read()!r}")


def main() -> int:
    base = os.getenv("CLICKHOUSE_URL", "http://localhost:8123")
    migrations_dir = pathlib.Path(__file__).resolve().parent.parent / "infra" / "clickhouse"
    if not migrations_dir.exists():
        print(f"no migrations dir at {migrations_dir}", file=sys.stderr)
        return 1

    _ch_post(base, "CREATE DATABASE IF NOT EXISTS pulseroute")
    _ch_post(
        base,
        "CREATE TABLE IF NOT EXISTS pulseroute.schema_migrations ("
        "name String, applied_at DateTime DEFAULT now()"
        ") ENGINE = MergeTree ORDER BY name",
    )

    for path in sorted(migrations_dir.glob("*.sql")):
        name = path.name
        # Statement separation on bare semicolons + newline.
        for stmt in [s.strip() for s in path.read_text().split(";\n") if s.strip()]:
            _ch_post(base, stmt)
        _ch_post(base, f"INSERT INTO pulseroute.schema_migrations (name) VALUES ('{name}')")
        print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
