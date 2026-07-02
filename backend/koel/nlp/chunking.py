"""Recursive text chunking for KB ingestion (X).

Splits a document into ~token-bounded passages on natural boundaries
(paragraphs → sentences → words) so each embedded chunk is coherent and small
enough for high-recall retrieval. Token count is approximated as chars/4 to keep
the splitter dependency-free (no tokenizer download); good enough for sizing.
"""

from __future__ import annotations

import re

_CHARS_PER_TOKEN = 4
_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?।])\s+")  # includes the Devanagari danda ।


def _split_long(segment: str, max_chars: int) -> list[str]:
    """Hard-wrap a segment that has no sentence break within budget, on words."""
    words = segment.split()
    out: list[str] = []
    cur = ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if len(candidate) > max_chars and cur:
            out.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, *, max_tokens: int = 512, overlap_tokens: int = 32) -> list[str]:
    """Chunk ``text`` into passages of at most ``max_tokens`` (approx), with a
    small trailing overlap carried into the next chunk to preserve context across
    boundaries. Returns [] for empty input.
    """
    text = (text or "").strip()
    if not text:
        return []
    max_chars = max_tokens * _CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _CHARS_PER_TOKEN

    # Break into the smallest natural units first (sentences within paragraphs),
    # further splitting any oversized sentence on word boundaries.
    units: list[str] = []
    for para in _PARA.split(text):
        for sent in _SENT.split(para.strip()):
            sent = sent.strip()
            if not sent:
                continue
            units.extend(_split_long(sent, max_chars) if len(sent) > max_chars else [sent])

    chunks: list[str] = []
    cur = ""
    for unit in units:
        candidate = f"{cur} {unit}".strip()
        if len(candidate) > max_chars and cur:
            chunks.append(cur)
            tail = cur[-overlap_chars:] if overlap_chars else ""
            cur = f"{tail} {unit}".strip()
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks
