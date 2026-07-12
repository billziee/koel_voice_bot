"""Carrier bridge contract (1.1 Twilio, 1.4 Exotel).

Extracting this interface up front means Exotel is a second implementation, not
a fork: everything above the bridge (orchestrator, STT/NLP/TTS, barge-in) is
shared, and only the carrier-specific WS framing differs.
"""

from __future__ import annotations

from typing import Protocol


class CarrierBridge(Protocol):
    """What the orchestrator drives to run a call over any PSTN carrier."""

    async def send_audio(self, pcm16: bytes, sample_rate: int) -> None:
        """Play synthesized PCM to the caller (the adapter transcodes to µ-law)."""
        ...

    async def send_clear(self) -> None:
        """Flush audio the carrier has buffered but not yet played — a confirmed
        barge-in must silence the bot immediately (PRD §05)."""
        ...

    async def hangup(self) -> None:
        """End the PSTN leg."""
        ...
