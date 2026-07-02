"""Telephony µ-law transcode seam (F)."""

from __future__ import annotations

import numpy as np

from koel.tts.mixer import _to_wav, to_ulaw_8k


def test_to_ulaw_downsamples_to_8k_mono_one_byte_per_sample():
    # 24 kHz mono WAV, 0.5s → after 8 kHz µ-law we expect ~1 byte per 8k sample.
    sr = 24000
    samples = np.zeros(sr // 2, dtype=np.int16)  # 0.5s of silence
    wav = _to_wav(samples.tobytes(), sr)

    ulaw = to_ulaw_8k(wav)

    expected = (sr // 2) * 8000 // sr  # resampled sample count, 1 byte each (µ-law)
    assert abs(len(ulaw) - expected) <= 2  # ±2 for resampler edge handling
    # µ-law silence encodes to 0xFF (or 0x7F); definitely not raw 16-bit PCM.
    assert len(ulaw) < len(samples.tobytes())


def test_to_ulaw_preserves_signal_energy_roughly():
    sr = 24000
    t = np.linspace(0, 0.25, sr // 4, endpoint=False)
    tone = (0.3 * 32767 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    ulaw = to_ulaw_8k(_to_wav(tone.tobytes(), sr))
    # A non-silent tone must not collapse to a constant byte.
    assert len(set(ulaw)) > 4
