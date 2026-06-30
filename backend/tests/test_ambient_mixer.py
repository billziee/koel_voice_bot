"""Tests for the synthesized call-floor ambient mixer (PRD §06)."""

from __future__ import annotations

import numpy as np

from koel.tts.mixer import _read_wav, _synth_bed, _to_wav, mix_into_wav, wav_sample_rate


def _silent_wav(seconds: float, sr: int = 24000) -> bytes:
    return _to_wav(b"\x00\x00" * int(sr * seconds), sr)


def test_bed_is_band_limited_babble_not_flat_white_noise():
    sr = 24000
    bed = _synth_bed("callfloor", sr)
    assert bed.size > 0 and float(np.max(np.abs(bed))) <= 1.0
    spec = np.abs(np.fft.rfft(bed))
    freqs = np.fft.rfftfreq(bed.size, 1.0 / sr)
    speech = spec[(freqs >= 300) & (freqs <= 3400)].mean()
    above = spec[freqs > 4500].mean()
    # Energy concentrated in the speech band → murmur, not flat white noise.
    assert speech > above * 5


def test_mix_adds_energy_under_silence_and_preserves_format():
    wav = _silent_wav(1.0)
    mixed = mix_into_wav(wav, "callfloor", gain=0.06)
    assert mixed[:4] == b"RIFF" and wav_sample_rate(mixed) == 24000
    base_pcm, _, _, _ = _read_wav(wav)
    mix_pcm, _, _, _ = _read_wav(mixed)
    assert mix_pcm.size == base_pcm.size  # same duration
    assert int(np.abs(mix_pcm.astype("int64")).sum()) > 0  # bed is audible


def test_silent_and_unknown_profiles_are_noops():
    wav = _silent_wav(0.5)
    assert mix_into_wav(wav, "silent", 0.06) == wav
    assert mix_into_wav(wav, "nope", 0.06) == wav
    assert mix_into_wav(wav, None, 0.06) == wav


def test_profile_gain_ordering():
    wav = _silent_wav(1.0)

    def rms(b: bytes) -> float:
        pcm, _, _, _ = _read_wav(b)
        return float(np.sqrt(np.mean((pcm.astype("float64")) ** 2)))

    # callfloor is louder/denser than office than comfort.
    assert rms(mix_into_wav(wav, "callfloor", 0.06)) > rms(mix_into_wav(wav, "office", 0.06))
    assert rms(mix_into_wav(wav, "office", 0.06)) > rms(mix_into_wav(wav, "comfort", 0.06))
