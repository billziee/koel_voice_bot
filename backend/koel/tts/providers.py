"""TTS provider interface + stub.

The stub emits silent PCM sized to the text length so the frontend's waveform
and playback timing behave realistically without a real vocoder. The real
provider streams from an Orpheus SageMaker endpoint with the same chunk shape.
"""

from __future__ import annotations

import base64
import logging
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from koel.tts.schemas import (
    TONE_PROSODY,
    Prosody,
    SynthesisChunk,
    SynthesisResult,
    SynthesizeRequest,
)

logger = logging.getLogger(__name__)


def _wav_duration_ms(wav: bytes) -> int:
    """Duration of a PCM WAV from its header (data size / byte rate)."""
    try:
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return 0
        byte_rate = struct.unpack_from("<I", wav, 28)[0]  # fmt: bytes/sec
        i = 12
        while i + 8 <= len(wav):
            cid = wav[i : i + 4]
            size = struct.unpack_from("<I", wav, i + 4)[0]
            if cid == b"data":
                return int(size / byte_rate * 1000) if byte_rate else 0
            i += 8 + size + (size & 1)
    except (struct.error, IndexError, ZeroDivisionError):
        return 0
    return 0

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


class GroqOrpheusProvider(TtsProvider):
    """Live neural TTS via Groq Orpheus (OpenAI-compatible ``/audio/speech``).

    Returns real WAV audio; ``duration_ms`` is parsed from the header so the
    no-input timer waits for actual playback. Requires one-time org-admin terms
    acceptance in the Groq console — until then (and on any error) it degrades to
    the stub so a live call never drops. The tone tag is preserved on the result
    for the schema; Orpheus voice selection is config-driven.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        base_url: str = "https://api.groq.com/openai/v1",
        audio_format: str = "wav",
        timeout_s: float = 30.0,
    ) -> None:
        self._model = model
        self._voice = voice
        self._format = audio_format
        self._url = f"{base_url.rstrip('/')}/audio/speech"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._fallback = StubOrpheusProvider()
        self._warned = False  # log the terms/credential failure once, not per turn

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": self._model,
                    "input": request.text,
                    "voice": self._voice,
                    "response_format": self._format,
                },
            )
            resp.raise_for_status()
            audio = resp.content
            if not audio or not resp.headers.get("content-type", "").startswith("audio"):
                raise ValueError("Orpheus returned no audio")
        except Exception:  # noqa: BLE001 — terms gate / network → degrade to stub
            if not self._warned:
                self._warned = True
                logger.warning(
                    "groq_orpheus_failed; falling back to stub (accept Orpheus terms in the "
                    "Groq console to enable real TTS). Suppressing further warnings.",
                    exc_info=True,
                )
            return await self._fallback.synthesize(request)

        duration = _wav_duration_ms(audio) or self._fallback._duration_ms(request.text, prosody)  # noqa: SLF001
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(audio).decode(),
            duration_ms=duration,
            prosody=prosody,
        )

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        # Orpheus /audio/speech returns a whole clip; emit it as one final chunk.
        result = await self.synthesize(request)
        yield SynthesisChunk(
            session_id=request.session_id, seq=0, audio_b64=result.audio_b64, is_final=True
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class OrpheusSageMakerProvider(TtsProvider):  # pragma: no cover - production stub
    def __init__(self, endpoint_name: str, region: str) -> None:
        self.endpoint_name = endpoint_name
        self.region = region

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        raise NotImplementedError("Invoke Orpheus SageMaker endpoint; map prosody → params.")

    def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        raise NotImplementedError("Stream from Orpheus with response-stream invoke.")
