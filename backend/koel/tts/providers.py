"""TTS provider interface + stub.

The stub emits silent PCM sized to the text length so the frontend's waveform
and playback timing behave realistically without a real vocoder. The real
provider streams from an Orpheus SageMaker endpoint with the same chunk shape.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from koel.tts.schemas import (
    TONE_PROSODY,
    Prosody,
    SynthesisChunk,
    SynthesisResult,
    SynthesizeRequest,
)

_SAMPLE_RATE = 16000
# ~60ms of speech per character is a rough but stable estimate for pacing.
_MS_PER_CHAR = 60


def resolve_prosody(request: SynthesizeRequest) -> Prosody:
    base = TONE_PROSODY[request.tone]
    return Prosody(
        pitch=base.pitch,
        rate=base.rate * request.speaking_rate,
        energy=base.energy,
    )


def _silent_pcm(duration_ms: int) -> bytes:
    samples = int(_SAMPLE_RATE * duration_ms / 1000)
    return b"\x00\x00" * samples  # 16-bit silence


def _ambient_pcm(duration_ms: int, gain: float) -> bytes:
    """Low-level comfort-noise bed (PRD §06 Ambient Audio Layer).

    Deterministic LCG noise so the bytes are reproducible (no RNG state) and the
    line carries call-floor texture instead of dead digital silence. In the stub
    the speech itself is silent, so this bed *is* the audible mix; the real
    Orpheus provider would sum this under the synthesized voice.
    """
    samples = int(_SAMPLE_RATE * duration_ms / 1000)
    amp = max(0, int(3000 * gain))  # peak amplitude of the bed
    if amp == 0:
        return _silent_pcm(duration_ms)
    out = bytearray()
    seed = 0x2545F4914F6CDD1D & 0x7FFFFFFF
    span_ = 2 * amp + 1
    for _ in range(samples):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        val = (seed % span_) - amp
        out += int(val).to_bytes(2, "little", signed=True)
    return bytes(out)


class TtsProvider(ABC):
    @abstractmethod
    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult: ...

    @abstractmethod
    def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]: ...


class StubOrpheusProvider(TtsProvider):
    """Generates correctly-sized silent audio so timing/latency look real."""

    def _duration_ms(self, text: str, prosody: Prosody) -> int:
        return max(200, int(len(text) * _MS_PER_CHAR / max(prosody.rate, 0.1)))

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        duration = self._duration_ms(request.text, prosody)
        if request.ambient and request.ambient_profile:
            pcm = _ambient_pcm(duration, request.ambient_gain)
        else:
            pcm = _silent_pcm(duration)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(pcm).decode(),
            duration_ms=duration,
            prosody=prosody,
        )

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        prosody = resolve_prosody(request)
        total = self._duration_ms(request.text, prosody)
        # Emit ~120ms chunks so VB_Svc can interrupt (TTS_KILL) at chunk boundaries.
        chunk_ms = 120
        emitted = 0
        seq = 0
        while emitted < total:
            this_ms = min(chunk_ms, total - emitted)
            emitted += this_ms
            yield SynthesisChunk(
                session_id=request.session_id,
                seq=seq,
                audio_b64=base64.b64encode(_silent_pcm(this_ms)).decode(),
                is_final=emitted >= total,
            )
            seq += 1


class OrpheusSageMakerProvider(TtsProvider):  # pragma: no cover - production stub
    def __init__(self, endpoint_name: str, region: str) -> None:
        self.endpoint_name = endpoint_name
        self.region = region

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        raise NotImplementedError("Invoke Orpheus SageMaker endpoint; map prosody → params.")

    def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        raise NotImplementedError("Stream from Orpheus with response-stream invoke.")
