"""Tests for the HDR-style log-bucketed histogram."""

from __future__ import annotations

import threading

import pytest
from pulseroute_shared.hdr import MAX_BUCKETS, HdrHistogram


def test_records_known_values_within_tolerance() -> None:
    """1000 evenly-spaced values: percentiles match within ~6% bucket error."""
    hdr = HdrHistogram()
    for v in range(1, 1001):
        hdr.record(v)

    assert hdr.total_count() == 1000
    # The histogram quantises to log buckets; tolerance accounts for that.
    assert hdr.percentile(0.50) == pytest.approx(500, rel=0.07)
    assert hdr.percentile(0.95) == pytest.approx(950, rel=0.07)
    assert hdr.percentile(0.99) == pytest.approx(990, rel=0.07)


def test_percentiles_batch_matches_individual_calls() -> None:
    hdr = HdrHistogram()
    for v in (1, 5, 10, 50, 100, 500, 1000, 5000, 10000):
        hdr.record(v)

    batch = hdr.percentiles(0.5, 0.9, 0.99)
    assert batch[0.5] == hdr.percentile(0.5)
    assert batch[0.9] == hdr.percentile(0.9)
    assert batch[0.99] == hdr.percentile(0.99)


def test_reset_zeros_buckets() -> None:
    hdr = HdrHistogram()
    for v in range(1, 101):
        hdr.record(v)
    assert hdr.total_count() == 100
    hdr.reset()
    assert hdr.total_count() == 0
    assert hdr.percentile(0.5) == 0


def test_concurrent_record_no_corruption() -> None:
    """8 threads x 10000 records each. All threads record the same value
    so the bucket count is deterministic; missing increments would mean
    a torn read/write under contention."""
    hdr = HdrHistogram()
    n_threads = 8
    n_records = 10_000
    value_us = 250

    def worker() -> None:
        for _ in range(n_records):
            hdr.record(value_us)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * n_records
    assert hdr.total_count() == expected
    # Single value -> one bucket has all the count.
    assert hdr.percentile(0.5) == pytest.approx(value_us, rel=0.07)


def test_export_prometheus_is_parseable() -> None:
    hdr = HdrHistogram()
    for v in (50, 100, 250, 500, 1000, 2500, 5000):
        for _ in range(10):
            hdr.record(v)

    text = hdr.export_prometheus(
        "pulseroute_cache_lookup_seconds",
        labels={"stage": "cache_lookup"},
        help_text="HDR-backed cache lookup latency.",
    )

    # Use the official Prometheus text parser to validate the format.
    from prometheus_client.parser import text_string_to_metric_families

    families = list(text_string_to_metric_families(text))
    assert len(families) == 1
    family = families[0]
    assert family.name == "pulseroute_cache_lookup_seconds"
    assert family.type == "histogram"

    # _count sample should equal the total recorded points.
    counts = [s for s in family.samples if s.name.endswith("_count")]
    assert counts and counts[0].value == 70


def test_zero_and_negative_values_collapse_to_zero_bucket() -> None:
    hdr = HdrHistogram()
    hdr.record(0)
    hdr.record(-1)
    assert hdr.total_count() == 2
    # Percentile on all-zero bucket returns the bucket midpoint, which is 0.
    assert hdr.percentile(0.5) == 0


def test_bucket_index_within_range() -> None:
    hdr = HdrHistogram()
    # Very large value should clamp to the last bucket and not overflow.
    hdr.record(10**15)
    counts = hdr._snapshot()  # type: ignore[attr-defined]
    assert sum(counts) == 1
    # Last populated bucket index should be < MAX_BUCKETS.
    last = max(i for i, c in enumerate(counts) if c > 0)
    assert last < MAX_BUCKETS
