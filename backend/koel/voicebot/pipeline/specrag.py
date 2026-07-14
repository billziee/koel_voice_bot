"""Speculative RAG (O-5) divergence check.

O-5 fires RAG retrieval EARLY — during the endpointing hold window, on the held
mid-thought fragment — so the chunks are already in hand when the final turn
runs. That is only safe to reuse if the final transcript is still asking the
same thing as the speculative query; otherwise the pre-fetched chunks are stale
and NLP_svc must retrieve for the real query instead.

This decides that. It is PURE and deterministic (no I/O, no clock) so it
unit-tests in isolation and adds nothing to the turn hot path but a set compare.
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")

# Token-overlap (Jaccard) at or above this ⇒ the two queries overlap enough to
# reuse the speculative chunks. Below it ⇒ diverged, re-retrieve. 0.6 tolerates a
# short follow-up appended to the held fragment (and minor STT wording drift)
# while still catching a genuinely different question.
_JACCARD_REUSE_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    """Lowercase, strip, and collapse internal whitespace runs to one space."""
    return _WS.sub(" ", text.strip().lower())


def transcripts_diverged(speculative: str, final: str) -> bool:
    """Whether ``final`` diverged from the ``speculative`` query enough that the
    pre-fetched chunks must NOT be reused.

    Returns ``False`` (reuse the chunks) when, after normalising (lowercase,
    strip, collapse whitespace):
      - the final is a prefix/continuation of the speculative query, or vice
        versa (the merge path appends the follow-up to the held fragment, so the
        final normally *starts with* the speculative text), or
      - their token sets overlap by Jaccard >= 0.6.

    Returns ``True`` (discard + re-retrieve) otherwise. An empty speculative
    query is always divergent (there is nothing to reuse), as is an empty final.
    """
    spec = _normalize(speculative)
    fin = _normalize(final)
    if not spec or not fin:
        return True
    # Prefix / continuation in either direction ⇒ same thought continuing.
    if spec.startswith(fin) or fin.startswith(spec):
        return False
    spec_tokens = set(spec.split())
    fin_tokens = set(fin.split())
    union = spec_tokens | fin_tokens
    if not union:
        return True
    jaccard = len(spec_tokens & fin_tokens) / len(union)
    return jaccard < _JACCARD_REUSE_THRESHOLD
