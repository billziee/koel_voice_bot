"""DTMF digit → synthetic customer turn mapping (roadmap 1.3).

A phone caller can answer by pressing keypad digits instead of speaking — an IVR
fallback for a noisy line, a hesitant caller, or accessibility. Twilio delivers
each press as a ``dtmf`` Media Streams frame (see ``telephony/twilio.py``); this
module turns a pressed digit into the customer utterance it stands for so the
keypress flows through the *exact same* orchestrator turn path as spoken input —
there is no separate IVR branch to keep in sync.

The mapping table is per-bot configuration (``BotConfig.config["dtmf_map"]``,
e.g. ``{"1": "Yes, I agree", "2": "No", "0": "I want to speak to an agent"}``),
so this module is pure and I/O-free: the map is passed in, an unknown digit maps
to ``None`` (the press is ignored, never a synthetic turn), and everything is
fully unit-testable without a carrier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Universal phone-keypad conventions for free-form numeric entry.
DTMF_TERMINATOR = "#"  # ends a multi-digit sequence
DTMF_CANCEL = "*"  # clears the in-progress sequence

# Bound multi-digit entry so a stuck/abusive keypad can't grow the buffer without
# limit (an account/reference number is comfortably under this).
_DEFAULT_MAX_DIGITS = 32


def dtmf_to_turn(digit: str, dtmf_map: Mapping[str, str]) -> str | None:
    """Map a single pressed digit to the customer utterance it represents.

    Returns the configured utterance, or ``None`` when the digit is not in the
    map — an unmapped (or empty) press is ignored rather than turned into a
    turn. An empty map therefore always yields ``None``.
    """
    if not digit:
        return None
    return dtmf_map.get(digit)


@dataclass(frozen=True)
class DtmfSequence:
    """Immutable accumulator for multi-digit keypad entry terminated by ``#``.

    For single-key menu choices ("press 1 to agree") use :func:`dtmf_to_turn`
    directly. This is for free-form numeric input (an amount, a reference or
    account number) where the caller types several digits and presses ``#``.

    ``press`` returns a NEW sequence plus the completed digit string once ``#``
    terminates entry (``None`` while still collecting). ``*`` cancels the current
    entry (the common "clear" convention). Non-digits and overflow past
    ``max_digits`` are ignored. Frozen so it is trivially testable and safe to
    share — every transition yields a fresh value.
    """

    digits: str = ""
    max_digits: int = _DEFAULT_MAX_DIGITS

    def press(self, digit: str) -> tuple[DtmfSequence, str | None]:
        """Apply one keypress; return ``(next_sequence, completed_or_None)``."""
        if digit == DTMF_TERMINATOR:
            # Terminate: emit what was collected (None if nothing was) and reset.
            return DtmfSequence(max_digits=self.max_digits), (self.digits or None)
        if digit == DTMF_CANCEL:
            return DtmfSequence(max_digits=self.max_digits), None
        if not digit or not digit.isdigit() or len(self.digits) >= self.max_digits:
            # Noise, a non-numeric key, or overflow — hold steady, emit nothing.
            return self, None
        return DtmfSequence(self.digits + digit, self.max_digits), None
