"""µ-law ↔ PCM codec for PSTN media (1.1).

Carriers stream 8 kHz mono G.711 µ-law; the pipeline works in 16-bit PCM. These
wrap the stdlib ``audioop`` (same dependency the ambient mixer already uses) so
inbound carrier audio can reach STT and synthesized audio can reach the carrier.
"""

from __future__ import annotations

_PCM_WIDTH = 2  # 16-bit samples


def pcm16_to_ulaw8k(pcm: bytes, sample_rate: int) -> bytes:
    """16-bit mono PCM at ``sample_rate`` → 8 kHz µ-law (what a carrier expects)."""
    import audioop  # noqa: stdlib (py311); localised so the deprecation stays contained

    if sample_rate != 8000:
        pcm, _ = audioop.ratecv(pcm, _PCM_WIDTH, 1, sample_rate, 8000, None)
    return audioop.lin2ulaw(pcm, _PCM_WIDTH)


def ulaw8k_to_pcm16(ulaw: bytes, target_rate: int = 16000) -> bytes:
    """8 kHz µ-law (inbound carrier audio) → 16-bit mono PCM at ``target_rate``
    (16 kHz by default, what the STT providers ingest)."""
    import audioop  # noqa: stdlib

    pcm = audioop.ulaw2lin(ulaw, _PCM_WIDTH)  # → 8 kHz PCM16
    if target_rate != 8000:
        pcm, _ = audioop.ratecv(pcm, _PCM_WIDTH, 1, 8000, target_rate, None)
    return pcm
