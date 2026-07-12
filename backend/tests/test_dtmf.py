"""DTMF digit → synthetic customer turn mapping (roadmap 1.3).

Pure, fast, carrier-free: the mapping table is passed in, so every case is a
plain function call.
"""

from __future__ import annotations

from koel.voicebot.telephony.dtmf import DtmfSequence, dtmf_to_turn

# A representative per-bot dtmf_map (BotConfig.config["dtmf_map"]).
_MAP = {"1": "Yes, I agree", "2": "No", "0": "I want to speak to an agent"}


# --- single-digit mapping ---------------------------------------------------

def test_maps_known_digit_to_its_utterance():
    assert dtmf_to_turn("1", _MAP) == "Yes, I agree"
    assert dtmf_to_turn("2", _MAP) == "No"
    assert dtmf_to_turn("0", _MAP) == "I want to speak to an agent"


def test_unknown_digit_returns_none():
    # An unmapped press is ignored — never a synthetic turn.
    assert dtmf_to_turn("9", _MAP) is None
    assert dtmf_to_turn("#", _MAP) is None


def test_empty_map_returns_none():
    assert dtmf_to_turn("1", {}) is None


def test_empty_digit_returns_none():
    assert dtmf_to_turn("", _MAP) is None


# --- multi-digit sequences (DtmfSequence) -----------------------------------

def test_sequence_collects_until_terminator():
    seq = DtmfSequence()
    completed = None
    for digit in ("4", "2", "0", "0", "#"):
        seq, completed = seq.press(digit)
    assert completed == "4200"
    # After a terminator the sequence resets for the next entry.
    assert seq.digits == ""


def test_sequence_returns_none_while_collecting():
    seq = DtmfSequence()
    seq, completed = seq.press("7")
    assert completed is None and seq.digits == "7"
    seq, completed = seq.press("8")
    assert completed is None and seq.digits == "78"


def test_cancel_clears_the_in_progress_entry():
    seq = DtmfSequence()
    seq, _ = seq.press("5")
    seq, completed = seq.press("*")
    assert completed is None and seq.digits == ""
    # A fresh entry after cancel still terminates cleanly.
    seq, _ = seq.press("9")
    seq, completed = seq.press("#")
    assert completed == "9"


def test_terminator_with_nothing_collected_is_none():
    seq, completed = DtmfSequence().press("#")
    assert completed is None


def test_non_digit_keys_are_ignored():
    seq = DtmfSequence()
    seq, completed = seq.press("A")  # a letter is not a keypad digit
    assert completed is None and seq.digits == ""


def test_sequence_is_bounded_by_max_digits():
    seq = DtmfSequence(max_digits=3)
    for digit in ("1", "2", "3", "4", "5"):  # overflow past 3 is dropped
        seq, _ = seq.press(digit)
    seq, completed = seq.press("#")
    assert completed == "123"


def test_sequence_is_immutable():
    original = DtmfSequence(digits="1")
    nxt, _ = original.press("2")
    # press returns a NEW value; the original is untouched.
    assert original.digits == "1"
    assert nxt.digits == "12"
    assert nxt is not original
