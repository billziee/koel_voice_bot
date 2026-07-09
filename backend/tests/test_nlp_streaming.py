"""First-token NLP streamer (roadmap O-2): incremental response_text sentence
extraction from a JSON-mode completion stream, with fail-safe fallback."""

from __future__ import annotations

import json

from koel.common.text import split_sentences
from koel.nlp.streaming import ResponseTextSentenceStreamer


def _feed_all(raw: str, *, step: int = 1) -> tuple[list[str], ResponseTextSentenceStreamer]:
    s = ResponseTextSentenceStreamer()
    out: list[str] = []
    for i in range(0, len(raw), step):
        out.extend(s.feed(raw[i : i + step]))
    return out, s


def test_emits_sentences_matching_full_split_char_by_char():
    text = "Namaste ji. Aap kaise hain? Main theek hoon."
    raw = json.dumps({"intent": "greeting", "response_text": text, "tone": "cheerful"})
    emitted, s = _feed_all(raw)
    # The stream reproduces exactly what json.loads would give — never diverges.
    assert emitted == split_sentences(text)
    assert not s.failed
    assert json.loads(s.buffer)["response_text"] == text


def test_response_text_last_key_still_extracted():
    text = "Ek baat. Doosri baat. Teesri baat."
    raw = json.dumps({"intent": "x", "tone": "neutral", "response_text": text})
    emitted, _ = _feed_all(raw)
    assert emitted == split_sentences(text)


def test_handles_escaped_quotes_newlines_and_danda():
    text = 'वह बोली। "haan ji" theek hai. Aur kuch?'
    raw = json.dumps({"response_text": text})  # escapes the inner quotes
    emitted, s = _feed_all(raw)
    assert emitted == split_sentences(text)
    assert json.loads(s.buffer)["response_text"] == text


def test_chunked_feeds_equivalent_to_char_by_char():
    text = "First sentence here. Second one now. Third and last!"
    raw = json.dumps({"response_text": text, "intent": "x"})
    emitted, _ = _feed_all(raw, step=7)  # arbitrary token-ish chunking
    assert emitted == split_sentences(text)


def test_holds_the_growing_final_sentence_until_closed():
    # Before the closing quote, the last (still-growing) sentence is withheld.
    s = ResponseTextSentenceStreamer()
    first = s.feed('{"response_text": "Done one. Growing tw')
    assert first == ["Done one."]  # complete sentence emitted, partial held
    rest = s.feed('o here."}')
    assert rest == ["Growing two here."]


def test_malformed_unicode_escape_fails_safe():
    raw = '{"response_text": "bad \\uZZZZ escape."}'
    emitted, s = _feed_all(raw)
    assert emitted == []  # nothing spoken
    assert s.failed  # caller falls back to the full parse


def test_lone_surrogate_escape_fails_safe():
    # A UTF-16 surrogate half can't be recombined escape-by-escape and is
    # unencodable; the streamer bails to the full-parse fallback rather than
    # emit an unencodable lone surrogate (would crash NDJSON serialization).
    raw = '{"response_text": "oops \\uD83D more."}'
    emitted, s = _feed_all(raw)
    assert emitted == []
    assert s.failed


def test_key_absent_emits_nothing_without_failing():
    raw = '{"intent": "x", "tone": "neutral"}'
    emitted, s = _feed_all(raw)
    assert emitted == []
    assert not s.failed  # no response_text yet — just no early sentences


def test_partial_escape_across_feed_boundary():
    s = ResponseTextSentenceStreamer()
    # Feed splits mid-escape (é = é); the streamer must hold, not corrupt.
    assert s.feed('{"response_text": "caf\\u00') == []
    out = s.feed('e9 ready. Go."}')
    assert out == ["café ready.", "Go."]
