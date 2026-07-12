"""TTS provider interface + stub.

The stub emits silent PCM sized to the text length so the frontend's waveform
and playback timing behave realistically without a real vocoder. The real
provider streams from an Orpheus SageMaker endpoint with the same chunk shape.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from koel.common.http import make_async_client, warm_connection
from koel.tts.mixer import mix_into_wav, to_ulaw_8k, wav_sample_rate
from koel.tts.schemas import (
    CANONICAL_SAMPLE_RATE,
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

_SAMPLE_RATE = CANONICAL_SAMPLE_RATE  # 24 kHz mono, canonical output rate
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


# split_sentences moved to koel.common.text (O-2) so TTS + NLP streaming share one
# boundary definition. Re-exported here so existing `from koel.tts.providers import
# split_sentences` call sites keep working.
from koel.common.text import split_sentences  # noqa: E402 — re-export after module docstring/consts


class TtsProvider(ABC):
    @abstractmethod
    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult: ...

    @abstractmethod
    def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]: ...

    async def warm(self) -> None:
        """Prime provider connection pools at session start (O-1). No-op by
        default; providers with a live HTTP client override it."""
        return None

    async def _stream_by_sentence(
        self, request: SynthesizeRequest
    ) -> AsyncIterator[SynthesisChunk]:
        """Synthesize each sentence and yield it as it's ready, so the caller can
        play the first sentence before the whole reply exists (Tier 2). Each chunk
        is a complete WAV — the browser decodes/plays them gaplessly."""
        sentences = split_sentences(request.text)
        for i, sentence in enumerate(sentences):
            result = await self.synthesize(request.model_copy(update={"text": sentence}))
            yield SynthesisChunk(
                session_id=request.session_id,
                seq=i,
                audio_b64=result.audio_b64,
                is_final=(i == len(sentences) - 1),
                duration_ms=result.duration_ms,
                sample_rate=result.sample_rate,
                audio_format=result.audio_format,
            )


class StubOrpheusProvider(TtsProvider):
    """Generates correctly-sized silent audio so timing/latency look real."""

    def _duration_ms(self, text: str, prosody: Prosody) -> int:
        return max(200, int(len(text) * _MS_PER_CHAR / max(prosody.rate, 0.1)))

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        duration = self._duration_ms(request.text, prosody)
        # Silent speech wrapped as WAV; the real ambient bed is mixed under it so
        # the stub carries authentic call-floor texture (not flat noise/silence).
        wav = _pcm_to_wav(_silent_pcm(duration), _SAMPLE_RATE)
        if request.ambient and request.ambient_profile:
            wav = mix_into_wav(wav, request.ambient_profile, request.ambient_gain)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(wav).decode(),
            duration_ms=duration,
            prosody=prosody,
            sample_rate=_SAMPLE_RATE,
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
        self._client = make_async_client(timeout_s=timeout_s)
        self._fallback = StubOrpheusProvider()
        self._warned = False  # log the terms/credential failure once, not per turn

    # Orpheus voices by gender (G). Per-bot voice_gender picks the persona; the
    # configured default is the fallback when gender is unset/unknown.
    _VOICE_BY_GENDER = {"female": "autumn", "male": "austin"}

    def _voice_for(self, request: SynthesizeRequest) -> str:
        # An explicit per-bot voice (4.2) wins over the gender-derived default.
        if request.voice:
            return request.voice
        return self._VOICE_BY_GENDER.get((request.voice_gender or "").lower(), self._voice)

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "model": self._model,
                    "input": request.text,
                    "voice": self._voice_for(request),
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
            result = await self._fallback.synthesize(request)
            result.fallback_used = True  # surface the degrade on the orchestrator span (E)
            return result

        duration = _wav_duration_ms(audio) or self._fallback._duration_ms(request.text, prosody)  # noqa: SLF001
        if request.ambient and request.ambient_profile:
            audio = mix_into_wav(audio, request.ambient_profile, request.ambient_gain)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(audio).decode(),
            duration_ms=duration,
            prosody=prosody,
            sample_rate=wav_sample_rate(audio) or CANONICAL_SAMPLE_RATE,
        )

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        # Orpheus /audio/speech returns a whole clip per call, so stream by
        # SENTENCE: first sentence plays while later ones synthesize (Tier 2).
        async for chunk in self._stream_by_sentence(request):
            yield chunk

    async def _stream_whole_clip(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        result = await self.synthesize(request)
        yield SynthesisChunk(
            session_id=request.session_id, seq=0, audio_b64=result.audio_b64, is_final=True
        )

    async def warm(self) -> None:
        await warm_connection(self._client, self._url)

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
        wav = _pcm_to_wav(pcm, self._sr)
        if request.ambient and request.ambient_profile:
            wav = mix_into_wav(wav, request.ambient_profile, request.ambient_gain)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(wav).decode(),
            duration_ms=int(len(pcm) / 2 / self._sr * 1000),
            prosody=prosody,
            sample_rate=self._sr,
        )

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        # Sentence-stream (Tier 2). A future enhancement is Riva synthesize_online
        # for true sub-sentence streaming; per-sentence already plays first-sentence-fast.
        async for chunk in self._stream_by_sentence(request):
            yield chunk


class SarvamTtsProvider(TtsProvider):
    """Hindi + Hinglish TTS via Sarvam AI's Bulbul model (REST) — the primary
    engine for both. Verified live: Bulbul voices romanized Hinglish text
    correctly (round-tripped through Sarvam STT at 1.0 language_probability),
    unlike Magpie which requires Devanagari (romanized comes out as gibberish).

    Fallback is language-aware because of that same script split: a hi request
    falls back to Magpie (also Devanagari-native — the correct script family),
    while a hinglish/en request falls straight to ``fallback`` (Orpheus/stub —
    Magpie would mangle romanized text). Never drops a turn either way.
    """

    _LANG_CODE = {"hi": "hi-IN", "hinglish": "hi-IN", "en": "en-IN"}

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        speaker: str,
        base_url: str = "https://api.sarvam.ai",
        audio_format: str = "wav",
        sample_rate: int = CANONICAL_SAMPLE_RATE,
        timeout_s: float = 20.0,
        fallback: TtsProvider,
        hi_fallback: TtsProvider | None = None,
        ws_enabled: bool = False,
    ) -> None:
        self._model = model
        self._speaker = speaker
        self._format = audio_format
        self._sr = sample_rate
        self._url = f"{base_url.rstrip('/')}/text-to-speech"
        self._headers = {"api-subscription-key": api_key, "Content-Type": "application/json"}
        self._api_key = api_key
        self._client = make_async_client(timeout_s=timeout_s)
        self._timeout_s = timeout_s
        self._fallback = fallback
        self._hi_fallback = hi_fallback
        self._warned = False
        self._ws_enabled = ws_enabled
        # https:// → wss:// on the same host; model + completion event are query params.
        ws_base = base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = f"{ws_base}/text-to-speech/ws?model={model}&send_completion_event=true"

    def _fallback_for(self, request: SynthesizeRequest) -> TtsProvider:
        if request.language.value == "hi" and self._hi_fallback is not None:
            return self._hi_fallback
        return self._fallback

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        prosody = resolve_prosody(request)
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "text": request.text,
                    "target_language_code": self._LANG_CODE.get(request.language.value, "hi-IN"),
                    "speaker": request.voice or self._speaker,  # per-bot persona (4.2)
                    "model": self._model,
                    "speech_sample_rate": self._sr,
                    "audio_format": self._format,
                },
            )
            resp.raise_for_status()
            audios = resp.json().get("audios") or []
            if not audios:
                raise ValueError("Sarvam returned no audio")
            wav = base64.b64decode(audios[0])
        except Exception:  # noqa: BLE001 — never drop a turn; degrade to fallback
            if not self._warned:
                self._warned = True
                logger.warning("sarvam_tts_failed; falling back", exc_info=True)
            result = await self._fallback_for(request).synthesize(request)
            result.fallback_used = True
            return result

        if request.ambient and request.ambient_profile:
            wav = mix_into_wav(wav, request.ambient_profile, request.ambient_gain)
        return SynthesisResult(
            session_id=request.session_id,
            audio_b64=base64.b64encode(wav).decode(),
            duration_ms=_wav_duration_ms(wav) or self._fallback_duration(request, prosody),
            prosody=prosody,
            sample_rate=wav_sample_rate(wav) or self._sr,
        )

    @staticmethod
    def _fallback_duration(request: SynthesizeRequest, prosody: Prosody) -> int:
        return max(200, int(len(request.text) * _MS_PER_CHAR / max(prosody.rate, 0.1)))

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        # WS streaming skips ambient turns: the bed is mixed per complete WAV,
        # and re-seeding it on every small WS chunk would make it stutter.
        if self._ws_enabled and not (request.ambient and request.ambient_profile):
            emitted = False
            try:
                async for chunk in self._stream_ws(request):
                    emitted = True
                    yield chunk
                return
            except Exception:  # noqa: BLE001 — degrade, never drop a turn
                if emitted:
                    # Part of the reply already reached the caller's ears; an
                    # HTTP retry would replay it from the start. End the turn.
                    logger.warning("sarvam_ws_failed_midstream; turn truncated", exc_info=True)
                    return
                logger.warning("sarvam_ws_failed; using http path", exc_info=True)
        async for chunk in self._stream_by_sentence(request):
            yield chunk

    async def _stream_ws(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        """True streaming via Sarvam's ``/text-to-speech/ws``: linear16 chunks
        arrive while synthesis runs, so first audio doesn't wait for the whole
        first sentence (protocol per docs.sarvam.ai text-to-speech/stream).

        Chunks are yielded the moment they arrive and are never held back to
        tag ``is_final`` — the consumer (orchestrator sentence-streaming loop)
        treats iterator termination as end-of-turn, and delaying the first
        chunk would tax exactly the time-to-first-audio this path exists for.
        """
        from websockets.asyncio.client import connect as ws_connect

        seq = 0
        async with ws_connect(
            self._ws_url,
            additional_headers={"api-subscription-key": self._api_key},
            open_timeout=self._timeout_s,
            close_timeout=2.0,
        ) as ws:
            config = {
                "target_language_code": self._LANG_CODE.get(request.language.value, "hi-IN"),
                "speaker": request.voice or self._speaker,  # per-bot persona (4.2)
                "speech_sample_rate": str(self._sr),
                "output_audio_codec": "linear16",
                "model": self._model,
            }
            await ws.send(json.dumps({"type": "config", "data": config}))
            await ws.send(json.dumps({"type": "text", "data": {"text": request.text}}))
            await ws.send(json.dumps({"type": "flush"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._timeout_s)
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "audio":
                    pcm = base64.b64decode(msg["data"]["audio"])
                    if not pcm:
                        continue
                    wav = _pcm_to_wav(pcm, self._sr)
                    yield SynthesisChunk(
                        session_id=request.session_id,
                        seq=seq,
                        audio_b64=base64.b64encode(wav).decode(),
                        is_final=False,
                        duration_ms=_wav_duration_ms(wav),
                        sample_rate=self._sr,
                    )
                    seq += 1
                elif mtype == "event" and msg.get("data", {}).get("event_type") == "final":
                    break
                elif mtype == "error":
                    raise ValueError(f"sarvam ws error: {msg.get('data')}")
        if seq == 0:
            raise ValueError("Sarvam WS returned no audio")

    async def warm(self) -> None:
        await warm_connection(self._client, self._url)

    async def aclose(self) -> None:
        await self._client.aclose()


class RoutingTtsProvider(TtsProvider):
    """Routes synthesis by call language: Hindi/Hinglish → ``hindi`` provider
    (NVIDIA Magpie), everything else → ``default`` (Groq Orpheus / stub)."""

    def __init__(self, *, default: TtsProvider, hindi: TtsProvider, hindi_languages: set[str]) -> None:
        self._default = default
        self._hindi = hindi
        self._langs = hindi_languages

    def _pick(self, request: SynthesizeRequest) -> tuple[TtsProvider, TtsProvider]:
        """Return (primary, fallback) for the request's language."""
        if request.language.value in self._langs:
            return self._hindi, self._default
        return self._default, self._hindi

    async def synthesize(self, request: SynthesizeRequest) -> SynthesisResult:
        primary, fallback = self._pick(request)
        try:
            result = await primary.synthesize(request)
        except Exception:  # noqa: BLE001 — cascade to the other engine, never drop a turn
            logger.warning(
                "tts_primary_failed_cascading_to_fallback session_id=%s language=%s",
                request.session_id, request.language.value, exc_info=True,
            )
            result = await fallback.synthesize(request)
            result.fallback_used = True
        return self._maybe_telephony(request, result)

    @staticmethod
    def _maybe_telephony(request: SynthesizeRequest, result: SynthesisResult) -> SynthesisResult:
        """Transcode post-mix audio to 8 kHz µ-law for a PSTN leg (F). No-op by default."""
        if not request.telephony:
            return result
        try:
            ulaw = to_ulaw_8k(base64.b64decode(result.audio_b64))
            result.audio_b64 = base64.b64encode(ulaw).decode()
            result.audio_format = "ulaw"
            result.sample_rate = 8000
        except Exception:  # noqa: BLE001 — never drop a turn on a transcode failure
            logger.warning("ulaw_transcode_failed session_id=%s", request.session_id, exc_info=True)
        return result

    async def stream(self, request: SynthesizeRequest) -> AsyncIterator[SynthesisChunk]:
        primary, _ = self._pick(request)
        async for chunk in primary.stream(request):
            yield chunk

    async def warm(self) -> None:
        # Warm both engines' pools — a call may route to either (default vs Hindi).
        for p in (self._default, self._hindi):
            await p.warm()

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
