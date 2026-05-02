"""Prompt normalisation for cache-key stability.

We want two requests that differ only in whitespace, system-prompt phrasing
quirks, or harmless punctuation to share a cache key. We do NOT want requests
that differ in meaningful payload (system role contents, message order) to
collide."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from pulseroute_shared.types import ChatMessage

_WS_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.strip().lower()
    text = _WS_RE.sub(" ", text)
    return text


def normalize_messages(messages: list[ChatMessage]) -> list[tuple[str, str]]:
    """Return a stable list of (role, normalized_content) tuples preserving order."""
    return [(m.role, _normalize_text(m.content)) for m in messages]


def prompt_fingerprint(messages: list[ChatMessage]) -> str:
    """Stable, deterministic 16-byte hex digest of the normalised conversation."""
    parts = "\n".join(f"{role}\x1f{content}" for role, content in normalize_messages(messages))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]
