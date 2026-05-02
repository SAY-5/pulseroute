"""Policy package tests: cost ceiling, content filter, PII redaction."""

from __future__ import annotations

from pulseroute_policies import ContentFilter, CostCeiling, redact_pii


def test_cost_ceiling_within_budget():
    r = CostCeiling(ceiling_usd_per_day=10.0).check(
        current_spend_usd=1.0, projected_request_cost=0.5
    )
    assert r.allowed
    assert r.reason == "within_budget"


def test_cost_ceiling_soft_threshold():
    r = CostCeiling(ceiling_usd_per_day=10.0, soft_threshold_pct=0.8).check(7.5, 0.5)
    assert r.allowed
    assert r.reason == "soft_threshold_reached"


def test_cost_ceiling_blocks_when_projected_exceeds():
    r = CostCeiling(ceiling_usd_per_day=10.0).check(9.5, 1.0)
    assert not r.allowed
    assert r.reason == "ceiling_exceeded"


def test_cost_ceiling_zero_means_unlimited():
    r = CostCeiling(ceiling_usd_per_day=0.0).check(99999.0, 1.0)
    assert r.allowed
    assert r.reason == "no_ceiling"


def test_content_filter_passes_clean_text():
    r = ContentFilter(blocked_terms=frozenset({"verboten"})).check("hello world")
    assert r.allowed


def test_content_filter_blocks_term():
    r = ContentFilter(blocked_terms=frozenset({"verboten"})).check("hello verboten world")
    assert not r.allowed
    assert r.reason == "blocked_term"


def test_content_filter_blocks_oversized():
    r = ContentFilter(max_chars=10).check("x" * 100)
    assert not r.allowed
    assert r.reason == "max_length_exceeded"


def test_redact_email():
    r = redact_pii("contact me at user@example.com please")
    assert "[REDACTED_EMAIL]" in r.text
    assert "user@example.com" not in r.text
    assert r.redacted_count == 1


def test_redact_ssn():
    r = redact_pii("SSN: 123-45-6789")
    assert "[REDACTED_SSN]" in r.text
    assert "123-45-6789" not in r.text


def test_redact_credit_card():
    r = redact_pii("card 4111 1111 1111 1111")
    assert "[REDACTED_CC]" in r.text


def test_redact_no_pii_is_passthrough():
    r = redact_pii("just a normal sentence")
    assert r.redacted_count == 0
    assert r.text == "just a normal sentence"
