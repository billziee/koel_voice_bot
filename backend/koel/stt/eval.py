"""Speech-recognition quality metrics (AC).

Word Error Rate over token sequences — the standard STT accuracy measure against
the PRD §14 >90% (WER < 0.10) target. Pure/dependency-free so it can run in CI
over a small labelled fixture set.
"""

from __future__ import annotations


def _tokens(text: str) -> list[str]:
    return text.lower().split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER = (substitutions + insertions + deletions) / reference words.

    0.0 is perfect; >1.0 is possible when the hypothesis is much longer than the
    reference. An empty reference returns 0.0 for an empty hypothesis, else 1.0.
    """
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    # Levenshtein distance over words (classic DP edit distance).
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i]
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1] / len(ref)


def corpus_wer(pairs: list[tuple[str, str]]) -> float:
    """Aggregate WER: total edits over total reference words (not a mean of ratios)."""
    total_ref = total_edits = 0
    for reference, hypothesis in pairs:
        ref_len = len(_tokens(reference))
        total_ref += ref_len
        total_edits += round(word_error_rate(reference, hypothesis) * max(ref_len, 1))
    return total_edits / total_ref if total_ref else 0.0
