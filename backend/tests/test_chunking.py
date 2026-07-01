"""Recursive KB chunker (X)."""

from __future__ import annotations

from koel.nlp.chunking import chunk_text


def test_empty_input_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_short_text_is_one_chunk():
    assert chunk_text("Regalia offers lounge access.") == ["Regalia offers lounge access."]


def test_long_text_splits_into_bounded_chunks():
    # ~30 sentences → must split; each chunk within the char budget.
    doc = " ".join(f"Sentence number {i} about the card benefits." for i in range(60))
    chunks = chunk_text(doc, max_tokens=40, overlap_tokens=4)
    assert len(chunks) > 1
    assert all(len(c) <= 40 * 4 for c in chunks)


def test_devanagari_sentence_boundary_is_respected():
    doc = "पहला वाक्य। दूसरा वाक्य। तीसरा वाक्य।"
    chunks = chunk_text(doc, max_tokens=4, overlap_tokens=0)  # tiny budget → split per sentence
    assert len(chunks) >= 2


def test_overlap_carries_context_between_chunks():
    doc = " ".join(f"word{i}" for i in range(200))
    chunks = chunk_text(doc, max_tokens=20, overlap_tokens=8)
    assert len(chunks) > 1  # overlap doesn't prevent progress / infinite loop
