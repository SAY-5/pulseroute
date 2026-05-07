"""Property-based tests for prompt-fingerprint normalisation.

We assert two invariants:
  1. Whitespace/case-only perturbations of the same prompt collide on
     ``prompt_fingerprint`` (collisions are intended for the cache).
  2. Prompts that differ in non-whitespace tokens produce distinct
     fingerprints (no spurious collisions across distinct prompts).
"""

from __future__ import annotations

import re
import unicodedata

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pulseroute_cache.normalize import _normalize_text, prompt_fingerprint
from pulseroute_shared.types import ChatMessage

# Avoid Hypothesis-generated strings that normalize to empty (whitespace-only,
# zero-width chars, etc.) since the property is undefined there.
_NON_WS_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
        blacklist_characters="\x00\t\n\r",
    ),
    min_size=3,
    max_size=80,
).filter(lambda s: bool(_normalize_text(s)))

# A narrower alphabet for the case-folding collision test: ASCII printable
# plus standard whitespace. Outside ASCII, lower()/upper() can be non-idempotent
# (e.g. dotless ı, German ß), which would break the collision invariant — that
# is a known property of `str.lower()`, not a bug in the cache normalizer.
_ASCII_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters=" ",
        blacklist_characters="",
        max_codepoint=0x7F,
    ),
    min_size=3,
    max_size=80,
).filter(lambda s: bool(_normalize_text(s)))


def _perturb_whitespace_and_case(rng_seed: int, text: str) -> str:
    """Apply only collisions that the normalizer is documented to absorb:
       1. case folding (lower/upper),
       2. expanding existing whitespace into longer whitespace runs (which
          the normalizer collapses back to a single space),
       3. extra leading/trailing whitespace (strip()'d).
    We deliberately do NOT inject whitespace inside word characters because
    the normalizer does not delete inter-token whitespace."""
    import random

    rng = random.Random(rng_seed)
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            out.append(rng.choice([" ", "  ", "\t", " \t ", "\n", " \n\t "]))
        else:
            out.append(ch.upper() if rng.random() < 0.5 else ch.lower())
    perturbed = "".join(out)
    perturbed = "  " + perturbed + " \t "
    return perturbed


@given(text=_ASCII_TEXT, seed=st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_whitespace_and_case_perturbations_collide(text: str, seed: int) -> None:
    a = [ChatMessage(role="user", content=text)]
    b = [ChatMessage(role="user", content=_perturb_whitespace_and_case(seed, text))]
    assert prompt_fingerprint(a) == prompt_fingerprint(b)


@given(
    text_a=_NON_WS_TEXT,
    text_b=_NON_WS_TEXT,
)
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_distinct_normalised_tokens_produce_distinct_fingerprints(
    text_a: str,
    text_b: str,
) -> None:
    if _normalize_text(text_a) == _normalize_text(text_b):
        return  # equivalent under normalisation; skip
    a = [ChatMessage(role="user", content=text_a)]
    b = [ChatMessage(role="user", content=text_b)]
    assert prompt_fingerprint(a) != prompt_fingerprint(b)


@given(text=_NON_WS_TEXT)
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_nfkc_compatibility_chars_collide(text: str) -> None:
    # NFKC maps compatibility characters (e.g. fullwidth digits, ligatures) to
    # their canonical form. If we substitute a character with a known
    # compatibility variant, the fingerprint must still collide.
    nfkc = unicodedata.normalize("NFKC", text)
    # Pick a substitution: fullwidth ASCII -> halfwidth.
    swapped = re.sub(
        r"[A-Za-z0-9]",
        lambda m: (
            chr(ord(m.group(0)) + 0xFEE0)
            if ord(m.group(0)) < 0x80 and 0x21 <= ord(m.group(0)) <= 0x7E
            else m.group(0)
        ),
        nfkc,
        count=1,
    )
    a = [ChatMessage(role="user", content=nfkc)]
    b = [ChatMessage(role="user", content=swapped)]
    assert prompt_fingerprint(a) == prompt_fingerprint(b)
