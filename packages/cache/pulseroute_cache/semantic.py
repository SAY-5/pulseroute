"""Semantic cache backed by Redis.

We keep two structures per tenant:
    pulseroute:cache:{tenant}:entries   hash  fp -> json(entry)
    pulseroute:cache:{tenant}:vectors   hash  fp -> json(vector)

Lookup is O(N) over the tenant's vectors. For the workload sizes typical of a
demo (≤ few thousand entries) this is fine; for production swap in RediSearch
with HNSW. The interface stays the same."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import redis.asyncio as aioredis
from pulseroute_shared.types import ChatMessage

from pulseroute_cache.embeddings import Embedder, cosine
from pulseroute_cache.normalize import prompt_fingerprint


@dataclass(slots=True)
class CacheEntry:
    fingerprint: str
    completion: str
    model: str
    created_at: float
    prompt_tokens: int
    completion_tokens: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "completion": self.completion,
                "model": self.model,
                "created_at": self.created_at,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> CacheEntry:
        return cls(**json.loads(raw))


@dataclass(slots=True)
class CacheLookup:
    hit: bool
    similarity: float
    entry: CacheEntry | None


class SemanticCache:
    def __init__(
        self,
        redis: aioredis.Redis,
        embedder: Embedder,
        threshold: float = 0.97,
        ttl_s: int = 60 * 60 * 24 * 7,
    ) -> None:
        self._r = redis
        self._embedder = embedder
        self.threshold = threshold
        self._ttl_s = ttl_s

    @staticmethod
    def _entries_key(tenant_id: str) -> str:
        return f"pulseroute:cache:{tenant_id}:entries"

    @staticmethod
    def _vectors_key(tenant_id: str) -> str:
        return f"pulseroute:cache:{tenant_id}:vectors"

    async def lookup(self, tenant_id: str, messages: list[ChatMessage]) -> CacheLookup:
        fp = prompt_fingerprint(messages)
        joined = "\n".join(m.content for m in messages)
        query_vec = self._embedder.embed(joined)

        # Exact-fingerprint fast path.
        raw_entry = await self._r.hget(self._entries_key(tenant_id), fp)
        if raw_entry:
            return CacheLookup(hit=True, similarity=1.0, entry=CacheEntry.from_json(raw_entry))

        # Semantic scan.
        all_vectors = await self._r.hgetall(self._vectors_key(tenant_id))
        best_fp: str | None = None
        best_sim = -1.0
        for stored_fp, stored_vec_raw in all_vectors.items():
            stored_vec = json.loads(stored_vec_raw)
            sim = cosine(query_vec, stored_vec)
            if sim > best_sim:
                best_sim = sim
                best_fp = stored_fp.decode() if isinstance(stored_fp, bytes) else stored_fp

        if best_fp is None or best_sim < self.threshold:
            return CacheLookup(hit=False, similarity=max(best_sim, 0.0), entry=None)

        raw = await self._r.hget(self._entries_key(tenant_id), best_fp)
        if not raw:
            return CacheLookup(hit=False, similarity=best_sim, entry=None)
        return CacheLookup(hit=True, similarity=best_sim, entry=CacheEntry.from_json(raw))

    async def store(
        self,
        tenant_id: str,
        messages: list[ChatMessage],
        completion: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> CacheEntry:
        fp = prompt_fingerprint(messages)
        entry = CacheEntry(
            fingerprint=fp,
            completion=completion,
            model=model,
            created_at=time.time(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        joined = "\n".join(m.content for m in messages)
        vec = self._embedder.embed(joined)

        pipe = self._r.pipeline()
        pipe.hset(self._entries_key(tenant_id), fp, entry.to_json())
        pipe.hset(self._vectors_key(tenant_id), fp, json.dumps(vec))
        pipe.expire(self._entries_key(tenant_id), self._ttl_s)
        pipe.expire(self._vectors_key(tenant_id), self._ttl_s)
        await pipe.execute()
        return entry
