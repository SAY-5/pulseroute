"""Embedding adapters for the semantic cache.

Both implementations are deterministic and dependency-free so unit tests do not
have to download or mock a model. They are NOT meant to give linguistic
similarity signal in production — wire a sentence-transformers model in a
Production embedder if you need that. They DO preserve the property that
identical normalised prompts produce identical vectors, which is what the cache
needs for the demo."""

from __future__ import annotations

import hashlib
import math
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Bag-of-token-hashes projected onto a fixed-size vector. Cheap, deterministic,
    and good enough that two paraphrases of the same factual question often map to
    nearby vectors because they share token vocabulary."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.split():
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 16) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class MockEmbedder:
    """Always returns the same vector. Used in cache-miss tests."""

    dim = 8

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [1.0 / math.sqrt(self.dim)] * self.dim


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
