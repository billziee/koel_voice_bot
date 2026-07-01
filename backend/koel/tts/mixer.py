"""Server-side synthesized call-floor ambience (PRD §06 Ambient Audio Layer).

Generates multi-talker **babble** — band-limited noise (the speech band) modulated
by slow syllabic envelopes and summed across many virtual talkers, plus sparse
phone-ring transients — which sounds like a contact-centre floor rather than the
flat white/LCG hiss it replaces. The bed is synthesized with numpy, cached per
(profile, sample_rate), made seamlessly loopable, then mixed UNDER the voice WAV
returned by any provider at a low per-profile gain.

Public API:
    mix_into_wav(wav_bytes, profile, gain) -> wav_bytes   # mix bed under voice
    wav_sample_rate(wav_bytes) -> int                     # read fmt sample rate
"""

from __future__ import annotations

import struct

import numpy as np

_BED_SECONDS = 8.0
_BAND_HZ = (300.0, 3400.0)  # telephone/speech band
_LOOP_FADE_S = 0.05

# Per-profile character: syllabic-AM "talker" layers, a linear gain multiplier
# applied on top of request.ambient_gain, and sparse ring events.
_PROFILES: dict[str, dict] = {
    "callfloor": {"talkers": 12, "mult": 1.0, "rings": 3},
    "office": {"talkers": 7, "mult": 0.5, "rings": 0},
    "comfort": {"talkers": 3, "mult": 0.33, "rings": 0},
    "silent": {"talkers": 0, "mult": 0.0, "rings": 0},
}

_cache: dict[tuple[str, int], np.ndarray] = {}


def _bandpass(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    spec[(freqs < lo) | (freqs > hi)] = 0.0
    return np.fft.irfft(spec, n=x.size)


def _synth_bed(profile: str, sr: int) -> np.ndarray:
    """Float32 babble bed in [-1, 1], length ~_BED_SECONDS, seamlessly loopable."""
    key = (profile, sr)
    if key in _cache:
        return _cache[key]
    cfg = _PROFILES.get(profile, _PROFILES["callfloor"])
    n = int(sr * _BED_SECONDS)
    if cfg["talkers"] == 0 or n <= 0:
        bed = np.zeros(max(0, n), dtype=np.float32)
        _cache[key] = bed
        return bed

    rng = np.random.default_rng(0xC0FFEE)  # deterministic but spectrally rich
    t = np.arange(n) / sr
    babble = np.zeros(n, dtype=np.float64)
    for _ in range(int(cfg["talkers"])):
        band = _bandpass(rng.standard_normal(n), sr, *_BAND_HZ)
        env = np.zeros(n)  # syllabic amplitude modulation ~3-7 Hz → murmur rhythm
        for _k in range(3):
            f = rng.uniform(3.0, 7.0)
            env += 0.5 + 0.5 * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))
        babble += band * (env / 3.0)

    for _ in range(int(cfg["rings"])):  # faint, fast-decaying phone rings
        start = int(rng.integers(0, n))
        idx = np.arange(start, min(start + int(sr * 0.4), n))
        local = (idx - start) / sr
        babble[idx] += np.sin(2 * np.pi * 440 * local) * np.exp(-local * 6.0) * 0.3

    peak = float(np.max(np.abs(babble))) or 1.0
    bed = (babble / peak).astype(np.float32)

    # Crossfade the tail into the head so tiling the loop has no seam click.
    fade = int(sr * _LOOP_FADE_S)
    if bed.size > 2 * fade > 0:
        w = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        bed[-fade:] = bed[-fade:] * (1.0 - w) + bed[:fade] * w

    _cache[key] = bed
    return bed


def _read_wav(wav: bytes) -> tuple[np.ndarray, int, int, int]:
    """(int16 PCM, sample_rate, channels, bits) from a PCM WAV. Raises on non-WAV."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a WAV")
    sr = channels = bits = 0
    data = b""
    i = 12
    while i + 8 <= len(wav):
        cid = wav[i : i + 4]
        size = struct.unpack_from("<I", wav, i + 4)[0]
        body = i + 8
        if cid == b"fmt ":
            channels = struct.unpack_from("<H", wav, body + 2)[0]
            sr = struct.unpack_from("<I", wav, body + 4)[0]
            bits = struct.unpack_from("<H", wav, body + 14)[0]
        elif cid == b"data":
            real = size if (size != 0xFFFFFFFF and body + size <= len(wav)) else len(wav) - body
            data = wav[body : body + real]
            break
        i = body + size + (size & 1)
    return np.frombuffer(data, dtype=np.int16), sr, channels, bits


def _to_wav(pcm: bytes, sr: int) -> bytes:
    byte_rate = sr * 2
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, byte_rate, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def wav_sample_rate(wav: bytes) -> int:
    try:
        return _read_wav(wav)[1]
    except (ValueError, struct.error, IndexError):
        return 0


def to_ulaw_8k(wav: bytes) -> bytes:
    """Transcode a PCM WAV to raw 8 kHz mono G.711 µ-law (PCMU) — the telephony
    seam (F). PSTN/Twilio Media Streams carry raw µ-law, not WAV, so this returns
    bare codec bytes. Default-off; only invoked when telephony mode is enabled.
    """
    import audioop  # stdlib (py311); localised so the deprecation stays contained

    pcm, sr, channels, _bits = _read_wav(wav)
    raw = pcm.tobytes()
    if channels and channels > 1:
        raw = audioop.tomono(raw, 2, 0.5, 0.5)
    if sr and sr != 8000:
        raw, _ = audioop.ratecv(raw, 2, 1, sr, 8000, None)
    return audioop.lin2ulaw(raw, 2)


def mix_into_wav(wav: bytes, profile: str | None, gain: float) -> bytes:
    """Mix the profile's ambient bed under a mono 16-bit PCM WAV. No-op on
    silent/unknown profiles, non-mono, or unparseable input — never raises."""
    if not profile or profile not in _PROFILES or _PROFILES[profile]["mult"] == 0.0:
        return wav
    try:
        pcm, sr, channels, bits = _read_wav(wav)
    except (ValueError, struct.error, IndexError):
        return wav
    if sr <= 0 or bits != 16 or channels != 1 or pcm.size == 0:
        return wav  # only mono 16-bit PCM is mixed (Orpheus/Magpie/stub are mono)

    bed_full = _synth_bed(profile, sr)
    if bed_full.size == 0:
        return wav
    reps = int(np.ceil(pcm.size / bed_full.size))
    bed = np.tile(bed_full, reps)[: pcm.size]

    voice = pcm.astype(np.float32) / 32768.0
    g = float(gain) * float(_PROFILES[profile]["mult"])
    mixed = np.clip(voice + bed * g, -1.0, 1.0)
    out = (mixed * 32767.0).astype("<i2").tobytes()
    return _to_wav(out, sr)
