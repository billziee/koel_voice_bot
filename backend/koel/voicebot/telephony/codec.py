"""µ-law ↔ PCM codec for PSTN media (1.1).

Carriers stream 8 kHz mono G.711 µ-law; the pipeline works in 16-bit PCM. These
wrap the stdlib ``audioop`` (same dependency the ambient mixer already uses) so
inbound carrier audio can reach STT and synthesized audio can reach the carrier.
"""

from __future__ import annotations

from typing import Any

_PCM_WIDTH = 2  # 16-bit samples

# audioop.ratecv's opaque continuation state (its internal resample-filter
# history); None means "start a fresh stream" (no prior history to carry).
ResampleState = Any


def pcm16_to_ulaw8k(
    pcm: bytes, sample_rate: int, resample_state: ResampleState = None
) -> tuple[bytes, ResampleState]:
    """16-bit mono PCM at ``sample_rate`` → 8 kHz µ-law (what a carrier expects).

    Returns ``(ulaw_bytes, new_resample_state)``. A caller resampling many
    consecutive chunks of ONE audio stream (e.g. one frame per Twilio media
    event) MUST thread the returned state into the next call — the resample
    filter's history otherwise resets every call, producing an audible
    discontinuity at every chunk boundary. Pass ``resample_state=None`` only
    when starting a new stream (or when ``sample_rate`` is already 8000, which
    skips resampling entirely).
    """
    import audioop  # noqa: stdlib (py311); localised so the deprecation stays contained

    if sample_rate != 8000:
        pcm, resample_state = audioop.ratecv(pcm, _PCM_WIDTH, 1, sample_rate, 8000, resample_state)
    else:
        resample_state = None
    return audioop.lin2ulaw(pcm, _PCM_WIDTH), resample_state


def ulaw8k_to_pcm16(
    ulaw: bytes, target_rate: int = 16000, resample_state: ResampleState = None
) -> tuple[bytes, ResampleState]:
    """8 kHz µ-law (inbound carrier audio) → 16-bit mono PCM at ``target_rate``
    (16 kHz by default, what the STT providers ingest).

    Returns ``(pcm_bytes, new_resample_state)`` — see ``pcm16_to_ulaw8k`` for why
    the state must be threaded back in across chunks of the same stream."""
    import audioop  # noqa: stdlib

    pcm = audioop.ulaw2lin(ulaw, _PCM_WIDTH)  # → 8 kHz PCM16
    if target_rate != 8000:
        pcm, resample_state = audioop.ratecv(pcm, _PCM_WIDTH, 1, 8000, target_rate, resample_state)
    else:
        resample_state = None
    return pcm, resample_state
