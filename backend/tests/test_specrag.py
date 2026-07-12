"""O-5 speculative RAG: the pure ``transcripts_diverged`` divergence check.

Decides whether a final transcript is close enough to the speculative query
(the held mid-thought fragment) to reuse the pre-fetched chunks. Prefix /
continuation or high token overlap ⇒ reuse (not diverged); unrelated text or an
empty speculative query ⇒ diverged (drop + re-retrieve)."""

from __future__ import annotations

from koel.voicebot.pipeline.specrag import transcripts_diverged


def test_continuation_is_not_diverged():
    # The merge path appends the follow-up to the held fragment, so the final
    # normally STARTS WITH the speculative query — reuse the chunks.
    assert transcripts_diverged("annual fee kitni hai", "annual fee kitni hai batao") is False


def test_final_prefix_of_speculative_is_not_diverged():
    # The other direction (final is a prefix of the speculative query) also reuses.
    assert transcripts_diverged("annual fee kitni hai batao", "annual fee kitni hai") is False


def test_identical_is_not_diverged():
    assert transcripts_diverged("mujhe card chahiye", "mujhe card chahiye") is False


def test_high_token_overlap_is_not_diverged():
    # No prefix relation, but Jaccard == 3/5 == 0.6 (>= threshold) ⇒ reuse.
    assert transcripts_diverged("annual fee kitni hai", "annual fee kitni batao") is False


def test_unrelated_text_is_diverged():
    assert transcripts_diverged("annual fee kitni hai", "mujhe personal loan chahiye") is True


def test_low_token_overlap_is_diverged():
    # Only one shared token out of a large union ⇒ below the 0.6 threshold.
    assert transcripts_diverged("annual fee kitni hai", "loan interest rate kitni") is True


def test_empty_speculative_is_diverged():
    # Nothing was pre-fetched worth reusing.
    assert transcripts_diverged("", "annual fee kitni hai") is True
    assert transcripts_diverged("   ", "annual fee") is True


def test_empty_final_is_diverged():
    assert transcripts_diverged("annual fee kitni hai", "") is True


def test_case_and_whitespace_insensitive():
    assert transcripts_diverged("Annual   FEE   kitni  Hai", "annual fee kitni hai") is False
    assert transcripts_diverged("  MUJHE Card  ", "mujhe card") is False
