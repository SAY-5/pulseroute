"""HDR-style log-bucketed latency histogram.

Pattern adapted from streamflow's LatencyHistogram (Java) and orderbook's
histogram.hpp (C++). Single-array of int counters indexed by:

    bucket = floor_log2(value_us) * SUB_BUCKETS + top_bits_below_leading_1

with SUB_BUCKETS=16 (4 bits below the leading 1). Yields ~6% relative error
per bucket, no allocation on record, lock contention only on the global
threading.Lock (Python lacks lock-free atomic ints without ctypes; the lock
is fine for study-scale and the contention impact is reported by the bench
harness).

API:
- record(value_microseconds: int) -- increment the matching bucket
- percentile(p: float) -> int -- microseconds at percentile p (0.0 to 1.0)
- percentiles(*ps) -> dict[float, int] -- batched
- reset() -- zero all buckets
- export_prometheus(name: str, labels: dict[str, str]) -> str -- Prometheus
  exposition format with _bucket lines at meaningful boundaries

Threading: a single threading.Lock guards the counter array on record()
and on snapshot reads. Real lock-free is unavailable in Python without
ctypes/native code; the lock is acceptable for study-scale and the bench
harness reports contention impact.
"""

from __future__ import annotations

import math
import threading

SUB_BUCKETS: int = 16
SUB_BITS: int = 4  # log2(SUB_BUCKETS)
MAX_POWER: int = 64
MAX_BUCKETS: int = MAX_POWER * SUB_BUCKETS  # 1024 buckets


def _bucket_index(value_us: int) -> int:
    """Map a microsecond value to its bucket index.

    Values <= 0 collapse to bucket 0. Values < SUB_BUCKETS map to a bucket
    equal to the value itself, which keeps small-value resolution at 1us
    granularity and avoids log2(0). Larger values use log2 of the value
    plus the top SUB_BITS below the leading 1.
    """
    if value_us <= 0:
        return 0
    if value_us < SUB_BUCKETS:
        return int(value_us)
    leading = value_us.bit_length() - 1  # floor(log2(value))
    # Top SUB_BITS below the leading 1.
    sub = (value_us >> (leading - SUB_BITS)) & (SUB_BUCKETS - 1)
    idx = leading * SUB_BUCKETS + sub
    if idx >= MAX_BUCKETS:
        return MAX_BUCKETS - 1
    return idx


def _bucket_lower_bound(idx: int) -> int:
    """Lower bound (inclusive) of the bucket in microseconds."""
    if idx < SUB_BUCKETS:
        return idx
    leading = idx // SUB_BUCKETS
    sub = idx % SUB_BUCKETS
    base = 1 << leading
    step = 1 << (leading - SUB_BITS)
    return base + sub * step


def _bucket_upper_bound(idx: int) -> int:
    """Upper bound (exclusive) of the bucket in microseconds."""
    if idx < SUB_BUCKETS:
        return idx + 1
    leading = idx // SUB_BUCKETS
    sub = idx % SUB_BUCKETS
    base = 1 << leading
    step = 1 << (leading - SUB_BITS)
    return base + (sub + 1) * step


def _bucket_midpoint(idx: int) -> int:
    """Representative value in microseconds for the bucket."""
    lower = _bucket_lower_bound(idx)
    upper = _bucket_upper_bound(idx)
    if idx == MAX_BUCKETS - 1:
        return lower + 1
    return int((lower + upper) / 2)


class HdrHistogram:
    """Log-bucketed histogram with microsecond inputs."""

    def __init__(self) -> None:
        self._counts: list[int] = [0] * MAX_BUCKETS
        self._lock = threading.Lock()

    def record(self, value_us: int) -> None:
        """Increment the bucket matching ``value_us``."""
        idx = _bucket_index(value_us)
        with self._lock:
            self._counts[idx] += 1

    def reset(self) -> None:
        """Zero all buckets."""
        with self._lock:
            for i in range(MAX_BUCKETS):
                self._counts[i] = 0

    def total_count(self) -> int:
        with self._lock:
            return sum(self._counts)

    def _snapshot(self) -> list[int]:
        with self._lock:
            return list(self._counts)

    def percentile(self, p: float) -> int:
        """Return the microsecond value at percentile ``p`` (0.0..1.0)."""
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"percentile must be in [0, 1], got {p}")
        counts = self._snapshot()
        total = sum(counts)
        if total == 0:
            return 0
        target = max(1, math.ceil(total * p))
        running = 0
        for idx, c in enumerate(counts):
            if c == 0:
                continue
            running += c
            if running >= target:
                return _bucket_midpoint(idx)
        # Should not reach here when total > 0; fall back to the last
        # populated bucket.
        for idx in range(MAX_BUCKETS - 1, -1, -1):
            if counts[idx] > 0:
                return _bucket_midpoint(idx)
        return 0

    def percentiles(self, *ps: float) -> dict[float, int]:
        """Compute multiple percentiles in a single pass over the buckets."""
        for p in ps:
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"percentile must be in [0, 1], got {p}")
        counts = self._snapshot()
        total = sum(counts)
        if total == 0:
            return dict.fromkeys(ps, 0)
        # Sort percentiles ascending so we walk the buckets exactly once.
        ordered = sorted((p, max(1, math.ceil(total * p))) for p in ps)
        out: dict[float, int] = {}
        running = 0
        bucket_iter = iter(enumerate(counts))
        cur_idx = -1
        cur_count = 0
        for p, target in ordered:
            while running < target:
                try:
                    cur_idx, cur_count = next(bucket_iter)
                except StopIteration:
                    break
                running += cur_count
            out[p] = _bucket_midpoint(cur_idx) if cur_idx >= 0 else 0
        return out

    def sum_us(self) -> int:
        """Approximate sum in microseconds using bucket midpoints."""
        counts = self._snapshot()
        total = 0
        for idx, c in enumerate(counts):
            if c == 0:
                continue
            total += c * _bucket_midpoint(idx)
        return total

    def export_prometheus(
        self,
        name: str,
        labels: dict[str, str] | None = None,
        help_text: str | None = None,
    ) -> str:
        """Render a Prometheus exposition-format histogram block.

        Bucket upper bounds are emitted in seconds (Prometheus convention).
        ``_sum`` and ``_count`` are emitted on the same metric. The bucket
        list is the union of populated buckets plus a ``+Inf`` terminator,
        keeping the output compact for sparse histograms.
        """
        counts = self._snapshot()
        total = sum(counts)
        if labels:
            label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            label_prefix = label_str + ","
            label_suffix = "{" + label_str + "}"
        else:
            label_prefix = ""
            label_suffix = ""

        lines: list[str] = []
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        else:
            lines.append(f"# HELP {name} HDR-backed latency histogram")
        lines.append(f"# TYPE {name} histogram")

        cumulative = 0
        approx_sum_seconds = 0.0
        for idx, c in enumerate(counts):
            cumulative += c
            if c == 0:
                continue
            upper_us = _bucket_upper_bound(idx)
            le = upper_us / 1_000_000.0
            lines.append(f'{name}_bucket{{{label_prefix}le="{_format_le(le)}"}} {cumulative}')
            approx_sum_seconds += c * (_bucket_midpoint(idx) / 1_000_000.0)

        lines.append(f'{name}_bucket{{{label_prefix}le="+Inf"}} {total}')
        lines.append(f"{name}_sum{label_suffix} {approx_sum_seconds:.9f}")
        lines.append(f"{name}_count{label_suffix} {total}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_le(le: float) -> str:
    # Prometheus accepts decimal floats; keep a stable, terse representation.
    if le >= 1.0:
        return f"{le:.6f}"
    return f"{le:.9f}"


__all__ = [
    "MAX_BUCKETS",
    "SUB_BUCKETS",
    "HdrHistogram",
]
