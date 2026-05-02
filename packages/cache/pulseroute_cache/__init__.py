"""Semantic cache abstraction backed by Redis."""

from pulseroute_cache.embeddings import HashEmbedder, MockEmbedder
from pulseroute_cache.normalize import normalize_messages, prompt_fingerprint
from pulseroute_cache.semantic import CacheEntry, CacheLookup, SemanticCache

__all__ = [
    "CacheEntry",
    "CacheLookup",
    "HashEmbedder",
    "MockEmbedder",
    "SemanticCache",
    "normalize_messages",
    "prompt_fingerprint",
]
