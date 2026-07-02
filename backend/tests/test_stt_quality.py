"""STT quality: WER metric + data-derived language confidence (AC)."""

from __future__ import annotations

from koel.stt.eval import corpus_wer, word_error_rate
from koel.stt.providers import GroqWhisperProvider


def test_wer_perfect_and_empty():
    assert word_error_rate("aap ka offer accha hai", "aap ka offer accha hai") == 0.0
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "spurious words") == 1.0


def test_wer_counts_edits():
    # 1 substitution out of 4 reference words → 0.25.
    assert word_error_rate("main aaj busy hoon", "main aaj free hoon") == 0.25
    # 1 deletion out of 3 → ~0.333.
    assert round(word_error_rate("ek do teen", "ek teen"), 3) == 0.333


def test_corpus_wer_aggregates_by_total_words():
    pairs = [("a b c d", "a b c d"), ("e f", "x f")]  # 0 + 1 edit over 6 words
    assert round(corpus_wer(pairs), 3) == round(1 / 6, 3)


def test_language_confidence_high_for_clean_speech():
    segments = [{"avg_logprob": -0.1, "no_speech_prob": 0.01} for _ in range(3)]
    assert GroqWhisperProvider._language_confidence(segments) > 0.85


def test_language_confidence_low_for_noisy_uncertain_audio():
    segments = [{"avg_logprob": -2.5, "no_speech_prob": 0.7}]
    assert GroqWhisperProvider._language_confidence(segments) < 0.3


def test_language_confidence_defaults_without_segments():
    assert GroqWhisperProvider._language_confidence([]) == 0.5
