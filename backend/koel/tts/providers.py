"""TTS provider interface + stub.

The stub emits silent PCM sized to the text length so the frontend's waveform
and playback timing behave realistically without a real vocoder. The real
provider streams from an Orpheus SageMaker endpoint with the same chunk shape.
"""

from __future__ import annotations

import asyncio
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
    """Duration of a PCM WAV from its header (data size / byte rate).

    Robust to chunk ordering (e.g. a LIST chunk before data) and to streaming
    writers like Orpheus that stamp a placeholder data size of 0xFFFFFFFF — in
    that case (or any size overrunning the buffer) the real byte count is taken
    from the remaining file length.
    """
    try:
        if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return 0
        byte_rate = 0
        i = 12
        while i + 8 <= len(wav):
            cid = wav[i : i + 4]
            size = struct.unpack_from("<I", wav, i + 4)[0]
            body = i + 8
            if cid == b"fmt ":
                byte_rate = struct.unpack_from("<I", wav, body + 8)[0]  # bytes/sec
            elif cid == b"data":
                if size == 0xFFFFFFFF or body + size > len(wav):
                    size = len(wav) - body  # placeholder / streamed → actual bytes
                return int(size / byte_rate * 1000) if byte_rate else 0
            i = body + size + (size & 1)
    except (struct.error, IndexError, ZeroDivisionError):
        return 0
    return 0


def _pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, sampwidth: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a WAV header (for providers that return PCM)."""
    byte_rate = sample_rate * channels * sampwidth
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
                                channels * sampwidth, sampwidth * 8)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
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


class NvidiaMagpieTtsProvider(TtsProvider):
    """Hindi/multilingual neural TTS via NVIDIA Magpie (NVCF Riva gRPC).

    Orpheus is English-only in practice; Magpie speaks hi-IN natively. Synthesis
    is a blocking gRPC call run in a worker thread; raw PCM is wrapped to WAV and
    the duration is exact. Any failure degrades to the supplied fallback provider.
    """

    _LANG_CODE = {"hi": "hi-IN", "hinglish": "hi-IN", "en": "en-US"}

    def __init__(
        self,
        *,
        api_key: str,
        function_id: str,
        uri: str,
        voice: str,
        sample_rate: int,
        timeout_s: float,
        fallback: TtsProvider,
    ) -> None:
        import riva.client  # lazy: only needed when NVIDIA TTS is enabled
        from riva.client.proto.riva_audio_pb2 import AudioEncoding

        self._encoding = AudioEncoding.LINEAR_PCM
        self._auth = riva.client.Auth(
            uri=uri,
            use_ssl=True,
            metadata_args=[
                ["function-id", function_id],
                ["authorization", f"Bearer {api_key}"],
            ],
        )
        self._svc = riva.client.SpeechSynthesisService(self._auth)
        self._voice = voice
        self._sr = sample_rate
        self._timeout = timeout_s
        self._fallback = fallback
        self._warned = False

    def _blocking_synth(self, text: str, language_code: str):
        return self._svc.synthesize(
            text, self._voice, language_code, sample_rate_hz=self._sr, encoding=self._encoding
        )

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        try:
            lang_code = self._LANG_CODE.get(request.language.value, "hi-IN")
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._blocking_synth, request.text, lang_code),
                timeout=self._timeout,
            )
            pcm = resp.audio
            if not pcm:
                raise ValueError("Magpie returned no audio")
        except Exception:  # noqa: BLE001 — never drop a turn; degrade to fallback
            if not self._warned:
                self._warned = True
                logger.warning("nvidia_magpie_failed; falling back", exc_info=True)
            return await self._fallback.synthesize(request)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(_pcm_to_wav(pcm, self._sr)).decode(),
            duration_ms=int(len(pcm) / 2 / self._sr * 1000),
            prosody=prosody,
        )

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        result = await self.synthesize(request)
        yield SynthesisChunk(
            session_id=request.session_id, seq=0, audio_b64=result.audio_b64, is_final=True
        )


class RoutingTtsProvider(TtsProvider):
    """Routes synthesis by call language: Hindi/Hinglish → ``hindi`` provider
    (NVIDIA Magpie), everything else → ``default`` (Groq Orpheus / stub)."""

    def __init__(self, *, default: TtsProvider, hindi: TtsProvider, hindi_languages: set[str]) -> None:
        self._default = default
        self._hindi = hindi
        self._langs = hindi_languages

    def _pick(self, request: SynthesizeRequest) -> TtsProvider:
        return self._hindi if request.language.value in self._langs else self._default

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        return await self._pick(request).synthesize(request)

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        async for chunk in self._pick(request).stream(request):
            yield chunk

    async def aclose(self) -> None:
        for p in (self._default, self._hindi):
            aclose = getattr(p, "aclose", None)
            if aclose is not None:
                await aclose()


class OrpheusSageMakerProvider(TtsProvider):  # pragma: no cover - production stub
    def __init__(self, endpoint_name: str, region: str) -> None:
        self.endpoint_name = endpoint_name
        self.region = region

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        raise NotImplementedError("Invoke Orpheus SageMaker endpoint; map prosody → params.")

    def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        raise NotImplementedError("Stream from Orpheus with response-stream invoke.")
