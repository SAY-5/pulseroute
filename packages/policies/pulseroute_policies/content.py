"""Lightweight content filter: blocklist + max-length checks. Heavier moderation
is intentionally delegated to the provider's own moderation endpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ContentFilterResult:
    allowed: bool
    reason: str
    matched_term: str | None = None


@dataclass
class ContentFilter:
    blocked_terms: frozenset[str] = field(default_factory=frozenset)
    max_chars: int = 200_000

    def check(self, text: str) -> ContentFilterResult:
        if len(text) > self.max_chars:
            return ContentFilterResult(False, "max_length_exceeded")
        lowered = text.lower()
        for term in self.blocked_terms:
            if term and re.search(rf"\b{re.escape(term.lower())}\b", lowered):
                return ContentFilterResult(False, "blocked_term", term)
        return ContentFilterResult(True, "ok")
