"""Semantic cache key normalisation + Redis-backed lookup behaviour."""

from __future__ import annotations

import pytest
from pulseroute_cache import normalize_messages, prompt_fingerprint
from pulseroute_shared.types import ChatMessage


def _ms(*pairs):
    return [ChatMessage(role=r, content=c) for r, c in pairs]


def test_whitespace_normalisation_collapses_to_same_fingerprint():
    a = prompt_fingerprint(_ms(("user", "Hello   world")))
    b = prompt_fingerprint(_ms(("user", "hello world")))
    assert a == b


def test_distinct_messages_produce_distinct_fingerprints():
    a = prompt_fingerprint(_ms(("user", "explain HTTP")))
    b = prompt_fingerprint(_ms(("user", "explain TCP")))
    assert a != b


def test_message_order_matters():
    a = prompt_fingerprint(_ms(("system", "be terse"), ("user", "hi")))
    b = prompt_fingerprint(_ms(("user", "hi"), ("system", "be terse")))
    assert a != b


def test_system_prompt_change_changes_fingerprint():
    a = prompt_fingerprint(_ms(("system", "be terse"), ("user", "hi")))
    b = prompt_fingerprint(_ms(("system", "be verbose"), ("user", "hi")))
    assert a != b


def test_normalize_returns_tuples_with_lowercased_content():
    out = normalize_messages(_ms(("user", "  Mixed  CASE  ")))
    assert out == [("user", "mixed case")]


@pytest.mark.asyncio
async def test_cache_store_and_exact_hit(semantic_cache):
    msgs = _ms(("user", "what is 2+2?"))
    await semantic_cache.store("t1", msgs, "4", "fake-large", 5, 1)
    lookup = await semantic_cache.lookup("t1", msgs)
    assert lookup.hit
    assert lookup.entry is not None
    assert lookup.entry.completion == "4"


@pytest.mark.asyncio
async def test_cache_miss_for_unrelated_prompt(semantic_cache):
    await semantic_cache.store("t1", _ms(("user", "what is 2+2?")), "4", "fake-large", 5, 1)
    lookup = await semantic_cache.lookup("t1", _ms(("user", "compare Postgres to ClickHouse")))
    assert not lookup.hit


@pytest.mark.asyncio
async def test_cache_isolation_per_tenant(semantic_cache):
    msgs = _ms(("user", "tenant-scoped data"))
    await semantic_cache.store("t1", msgs, "secret", "fake-large", 5, 1)
    lookup = await semantic_cache.lookup("t2", msgs)
    assert not lookup.hit
