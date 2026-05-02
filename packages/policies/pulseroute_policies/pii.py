"""Conservative PII redaction. Targets the high-confidence patterns only —
emails, US SSNs, and credit-card-shaped digit strings. Anything fancier should
go through a dedicated NER service."""

from __future__ import annotations

import re
from dataclasses import dataclass

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    redacted_count: int


def redact_pii(text: str) -> RedactionResult:
    """Return ``text`` with high-confidence PII replaced by typed placeholders."""
    count = 0

    def _sub(pattern: re.Pattern[str], placeholder: str, value: str) -> str:
        nonlocal count
        new, n = pattern.subn(placeholder, value)
        count += n
        return new

    text = _sub(_EMAIL_RE, "[REDACTED_EMAIL]", text)
    text = _sub(_SSN_RE, "[REDACTED_SSN]", text)
    text = _sub(_CC_RE, "[REDACTED_CC]", text)
    return RedactionResult(text=text, redacted_count=count)
