"""Shared text helpers.

``split_sentences`` was lifted here from ``tts/providers.py`` (roadmap O-2) so
both the sentence-streaming TTS path and the NLP first-token streamer share one
boundary definition (incl. the Devanagari danda) instead of drifting.
"""

from __future__ import annotations

import re

# Sentence boundary incl. the Devanagari danda — matches nlp/chunking's splitter.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?।])\s+")


def split_sentences(text: str) -> list[str]:
    """Split a reply into sentences for per-sentence streaming (latency)."""
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()] or [text]
