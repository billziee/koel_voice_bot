"""STT provider interface + stub implementation.

``WhisperSageMakerProvider`` is intentionally a stub: the real implementation
invokes a SageMaker Whisper endpoint. The interface is what the rest of the
system is built against, so swapping the body is a config change, not a rewrite.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from koel.common.http import warm_connection
from koel.common.schemas import Language
from koel.stt.schemas import TranscribeRequest, TranscriptResult

logger = logging.getLogger(__name__)

# Audio container → MIME, so Groq Whisper can identify the codec.
_AUDIO_MIME: dict[str, str] = {
    "wav": "audio/wav", "webm": "audio/webm", "m4a": "audio/mp4",
    "mp4": "audio/mp4", "mp3": "audio/mpeg", "ogg": "audio/ogg", "flac": "audio/flac",
}

# Whisper returns a language *name*; map the ones we operate in, else `other`
# (which trips the graceful unsupported-language fallback, PRD §06).
_WHISPER_LANG: dict[str, Language] = {
    "english": Language.en,
    "hindi": Language.hi,
}

# Force Whisper's transcription language from the bot's language (ISO-639-1) so it
# never auto-detects. Hinglish is Hindi-dominant code-mix → transcribe as Hindi
# (Whisper has no "hinglish"); it still captures embedded English banking terms.
_WHISPER_FORCE: dict[Language, str] = {
    Language.en: "en",
    Language.hi: "hi",
    Language.hinglish: "hi",
    # Additional Indian languages (4.1); Whisper large-v3 transcribes all of these.
    Language.ta: "ta",
    Language.te: "te",
    Language.mr: "mr",
    Language.bn: "bn",
}


class SttProvider(ABC):
    @abstractmethod
    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult: ...

    async def stream_transcribe(
        self, request: TranscribeRequest
    ) -> AsyncIterator[TranscriptResult]:
        """Streaming STT seam (O-6): yield transcripts as they resolve, marking
        interim results ``is_final=False`` and the last ``is_final=True``.

        The default wraps the batch :meth:`transcribe` and yields exactly one
        final result, so every provider satisfies the seam out of the box —
        partials are a per-provider optimization (see
        :meth:`SarvamSttProvider.stream_transcribe`). The consumer treats
        iterator termination as end-of-utterance.
        """
        result = await self.transcribe(request)
        # Batch honours request.is_final; force the terminal flag so the single
        # yielded result is unambiguously final regardless of the request.
        if not result.is_final:
            result = result.model_copy(update={"is_final": True})
        yield result

    async def warm(self) -> None:
        """Prime provider connection pools at session start (O-1). No-op by
        default; providers with a live HTTP client override it."""
        return None


# A handful of canned utterances keyed by audio length so the demo pipeline
# produces deterministic, varied transcripts without a real model. The orchestrator
# treats these exactly as it would Whisper output.
_DEMO_UTTERANCES: list[tuple[str, Language]] = [
    ("haan boliye", Language.hinglish),
    ("mujhe interest nahi hai", Language.hinglish),
    ("achha", Language.hinglish),  # backchannel — Gate 2 should ignore
    ("yes please tell me more", Language.en),
    ("koi bande se baat kara do", Language.hinglish),
]

# "Type-to-talk" sentinel: if the decoded audio is UTF-8 beginning with this
# prefix, the remainder is returned verbatim as the transcript. This lets the
# demo client and the frontend drive deterministic conversations (and exercise
# unsupported-language fallback) without a microphone or a real model. Real
# Whisper never sees this — it only matters to the stub.
_TEXT_SENTINEL = b"TEXT:"

# Heuristic language id for the type-to-talk path. Devanagari → Hindi; presence
# of common Hindi/romanised tokens alongside English → Hinglish; else English.
_HINDI_TOKENS = {
    "haan", "nahi", "hai", "kar", "karo", "mujhe", "aap", "main", "achha",
    "theek", "bande", "baat", "chahiye", "ji", "kijiye", "bhej", "kal",
}
_UNSUPPORTED_TOKENS = {  # Marathi/Bengali/Tamil markers → trigger fallback (PRD §06)
    "नमस्कार", "আমি", "வணக்கம்", "মারাঠি", "ব্যাংক",
}


def _detect_language(text: str) -> tuple[Language, float]:
    if any(tok in text for tok in _UNSUPPORTED_TOKENS):
        # Confident detection of a language outside EN/HI/Hinglish → fallback.
        return Language.other, 0.95
    has_devanagari = any("ऀ" <= ch <= "ॿ" for ch in text)
    words = set(text.lower().split())
    hindi_hits = len(words & _HINDI_TOKENS)
    ascii_words = [w for w in words if w.isascii() and w.isalpha()]
    if has_devanagari and hindi_hits == 0:
        return Language.hi, 0.95
    if hindi_hits and ascii_words:
        return Language.hinglish, 0.95
    if hindi_hits:
        return Language.hi, 0.93
    return Language.en, 0.96


class StubWhisperProvider(SttProvider):
    """Deterministic, dependency-free STT for local demo and CI."""

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, binascii.Error):
            raw = b""

        # Approx speech duration from byte length (16-bit PCM @ sample_rate).
        bytes_per_ms = max(1, (request.sample_rate * 2) // 1000)
        duration_ms = len(raw) // bytes_per_ms

        # Type-to-talk path: verbatim transcript from the sentinel payload.
        if raw.startswith(_TEXT_SENTINEL):
            text = raw[len(_TEXT_SENTINEL):].decode("utf-8", errors="replace").strip()
            lang, confidence = _detect_language(text)
            if request.language_hint:
                lang = request.language_hint
            # Estimate spoken duration so Gate 2 (barge-in) sees realistic length.
            duration_ms = max(duration_ms, len(text) * 60)
            return TranscriptResult(
                session_id=request.session_id,
                text=text if request.is_final else text.split(" ")[0],
                is_final=request.is_final,
                language_id=lang,
                language_confidence=confidence,
                speech_duration_ms=duration_ms,
                word_confidence=0.94,
            )

        idx = len(raw) % len(_DEMO_UTTERANCES)
        text, lang = _DEMO_UTTERANCES[idx]
        if request.language_hint:
            lang = request.language_hint

        return TranscriptResult(
            session_id=request.session_id,
            text=text if request.is_final else text.split(" ")[0],
            is_final=request.is_final,
            language_id=lang,
            language_confidence=0.96,
            speech_duration_ms=duration_ms or 120,
            word_confidence=0.93,
        )


class GroqWhisperProvider(SttProvider):
    """Live STT via Groq Whisper (OpenAI-compatible ``/audio/transcriptions``).

    Real audio bytes (any container Whisper accepts: wav/webm/m4a/mp3…) are
    POSTed as multipart; the ``verbose_json`` response yields text + language +
    duration. The ``TEXT:`` type-to-talk sentinel is still honoured (so the demo
    works without a mic), and any failure degrades to the deterministic stub so a
    live call never drops.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout_s: float = 30.0,
    ) -> None:
        self._model = model
        self._url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._fallback = StubWhisperProvider()

    @staticmethod
    def _confidence(segments: list[dict]) -> float:
        if not segments:
            return 0.9
        probs = [math.exp(s.get("avg_logprob", -0.5)) for s in segments]
        return max(0.0, min(1.0, sum(probs) / len(probs)))

    @staticmethod
    def _language_confidence(segments: list[dict]) -> float:
        """Data-derived language-detection confidence (AC).

        Whisper's verbose_json has no explicit language-probability field, so we
        proxy it from transcription quality: mean exp(avg_logprob) discounted by
        the mean no_speech_prob. High-quality, speech-dominant audio → high
        confidence; noise/uncertain segments → low (which lets the orchestrator's
        unsupported-language fallback trigger correctly instead of on a constant).
        """
        if not segments:
            return 0.5
        quality = sum(math.exp(s.get("avg_logprob", -0.7)) for s in segments) / len(segments)
        speech = 1.0 - sum(s.get("no_speech_prob", 0.0) for s in segments) / len(segments)
        return max(0.0, min(1.0, quality * speech))

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, binascii.Error):
            raw = b""
        # Type-to-talk passthrough keeps the keyboard-driven demo working.
        if raw.startswith(_TEXT_SENTINEL) or not raw:
            return await self._fallback.transcribe(request)

        fmt = (request.audio_format or "wav").lower()
        mime = _AUDIO_MIME.get(fmt, "application/octet-stream")
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                files={"file": (f"audio.{fmt}", raw, mime)},
                data={
                    "model": self._model,
                    "response_format": "verbose_json",
                    **({"language": _WHISPER_FORCE[request.language_hint]}
                       if request.language_hint in _WHISPER_FORCE else {}),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 — never drop a live turn; degrade to stub
            logger.warning(
                "groq_whisper_failed; falling back to stub",
                extra={"extra_fields": {"session_id": request.session_id}},
                exc_info=True,
            )
            return await self._fallback.transcribe(request)

        text = (data.get("text") or "").strip()
        segments = data.get("segments", [])
        lang = _WHISPER_LANG.get(str(data.get("language", "")).lower(), Language.other)
        if request.language_hint and lang is Language.other:
            lang = request.language_hint
        duration_ms = int(float(data.get("duration", 0)) * 1000)
        return TranscriptResult(
            session_id=request.session_id,
            text=text,
            is_final=request.is_final,
            language_id=lang,
            language_confidence=self._language_confidence(segments),  # AC: data-derived
            speech_duration_ms=duration_ms or max(120, len(text) * 60),
            word_confidence=self._confidence(segments),
        )

    async def warm(self) -> None:
        await warm_connection(self._client, self._url)

    async def aclose(self) -> None:
        await self._client.aclose()


# Sarvam's returned BCP-47 language code → our internal enum. Unlike Whisper
# (forced via language_hint, see _WHISPER_FORCE), Sarvam is called in
# auto-detect mode (language_code="unknown") specifically so it returns a
# genuine language_probability — that's what feeds the unsupported-language
# escalation floor (orchestrator._is_unsupported_language). Forcing it the
# way Whisper does would zero out that signal (Sarvam returns a null
# probability when a specific language is provided), silently disabling the
# escalation path.
_SARVAM_LANG: dict[str, Language] = {
    "hi-in": Language.hi,
    "en-in": Language.en,
}

# Sarvam WS STT ingests 16 kHz WAV (KOEL's mic-capture rate); the `encoding`
# field is a WAV-only enum (verified live).
_SARVAM_WS_SAMPLE_RATE = 16000


class SarvamSttProvider(SttProvider):
    """Live STT via Sarvam Saaras (multipart ``/speech-to-text``), primary for
    Hindi/code-mixed calls — Sarvam is trained on real-world Indian code-switched
    speech, unlike Whisper which needs language pinning to stay usable here.
    Any failure (or the ``TEXT:`` type-to-talk sentinel) falls through to
    ``fallback`` (Groq Whisper), which itself falls to the deterministic stub —
    a live call never drops.

    When ``stream_stt`` is enabled, :meth:`stream_transcribe` upgrades the seam
    to Sarvam's ``/speech-to-text/ws`` (partial + final transcripts stream out
    while audio frames stream in), mirroring the TTS WS path. Any WS error
    degrades to the batch :meth:`transcribe` (which itself cascades to the
    fallback → stub), so the streaming path never raises on a live call.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.sarvam.ai",
        mode: str = "transcribe",
        timeout_s: float = 20.0,
        fallback: SttProvider,
        stream_stt: bool = False,
    ) -> None:
        self._model = model
        self._mode = mode
        self._url = f"{base_url.rstrip('/')}/speech-to-text"
        self._headers = {"api-subscription-key": api_key}
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._timeout_s = timeout_s
        self._fallback = fallback
        self._warned = False
        self._stream_stt = stream_stt
        self._ws_warned = False
        # https:// → wss:// on the same host; model + mode ride as query params.
        ws_base = base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._ws_url = f"{ws_base}/speech-to-text/ws?model={model}&language_code=unknown"

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, binascii.Error):
            raw = b""
        # Type-to-talk passthrough / empty audio → skip the network call entirely.
        if raw.startswith(_TEXT_SENTINEL) or not raw:
            return await self._fallback.transcribe(request)

        fmt = (request.audio_format or "wav").lower()
        mime = _AUDIO_MIME.get(fmt, "application/octet-stream")
        try:
            resp = await self._client.post(
                self._url,
                headers=self._headers,
                files={"file": (f"audio.{fmt}", raw, mime)},
                data={"model": self._model, "mode": self._mode, "language_code": "unknown"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 — never drop a live turn; degrade to fallback
            if not self._warned:
                self._warned = True
                logger.warning(
                    "sarvam_stt_failed; falling back",
                    extra={"extra_fields": {"session_id": request.session_id}},
                    exc_info=True,
                )
            return await self._fallback.transcribe(request)

        text = (data.get("transcript") or "").strip()
        lang = _SARVAM_LANG.get(str(data.get("language_code", "")).lower(), Language.other)
        if request.language_hint and lang is Language.other:
            lang = request.language_hint
        confidence = data.get("language_probability")
        return TranscriptResult(
            session_id=request.session_id,
            text=text,
            is_final=request.is_final,
            language_id=lang,
            language_confidence=confidence if confidence is not None else 0.5,
            speech_duration_ms=max(120, len(text) * 60),
            word_confidence=0.9,  # Sarvam's REST response has no per-word score
        )

    async def stream_transcribe(
        self, request: TranscribeRequest
    ) -> AsyncIterator[TranscriptResult]:
        """Stream partials + a final over ``/speech-to-text/ws`` when the seam is
        enabled; otherwise (or on any WS error) degrade to the batch path.

        Degradation reuses the base default (batch :meth:`transcribe`, which
        cascades to the fallback → stub), so a live call is never dropped and
        the seam stays dormant when ``stream_stt`` is off.
        """
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, binascii.Error):
            raw = b""

        # Seam off, type-to-talk sentinel, or empty audio → skip the WS entirely.
        if self._stream_stt and raw and not raw.startswith(_TEXT_SENTINEL):
            produced_final = False
            try:
                async for result in self._stream_ws(request, raw):
                    produced_final = produced_final or result.is_final
                    yield result
            except Exception:  # noqa: BLE001 — never raise on the live path; degrade
                if not self._ws_warned:
                    self._ws_warned = True
                    logger.warning(
                        "sarvam_stt_ws_failed; degrading to batch transcribe",
                        extra={"extra_fields": {"session_id": request.session_id}},
                        exc_info=True,
                    )
            else:
                if produced_final:
                    return
                # WS closed without a final transcript → treat as incomplete and
                # fall through to the batch path for an authoritative result.
                logger.warning(
                    "sarvam_stt_ws_no_final; degrading to batch transcribe",
                    extra={"extra_fields": {"session_id": request.session_id}},
                )

        # Batch path (base default): one final result from transcribe() → fallback.
        async for result in SttProvider.stream_transcribe(self, request):
            yield result

    async def _stream_ws(
        self, request: TranscribeRequest, raw: bytes
    ) -> AsyncIterator[TranscriptResult]:
        """Transcribe over Sarvam's ``/speech-to-text/ws`` (protocol verified live,
        2026-07): send one ``{"audio": {"data": <base64 wav>, "encoding":
        "audio/wav", "sample_rate": 16000}}`` frame, then ``{"type": "flush"}``;
        Sarvam replies with ``{"type": "data", "data": {"transcript": ...,
        "language_code": ..., "language_probability": ...}}``. We send the buffered
        utterance as one WAV, so this yields a single final transcript over a
        persistent socket (lower per-turn overhead than the multipart REST POST).

        NOTE (O-6 scope): Sarvam's ``encoding`` is a WAV-only enum, so true
        token-level partials (which O-5 speculative RAG would consume) aren't
        cleanly available — that needs incremental-WAV chunking or a partial-
        emitting provider. The WS-handler ingest rewrite (feeding mic chunks in
        place of the batch buffer) remains the activation step; this method is the
        verified transport it would build on. Any WS error degrades to batch.
        """
        from websockets.asyncio.client import connect as ws_connect

        fmt = (request.audio_format or "wav").lower()
        async with ws_connect(
            self._ws_url,
            additional_headers={"api-subscription-key": self._api_key},
            open_timeout=self._timeout_s,
            close_timeout=2.0,
        ) as ws:
            # Verified live against Sarvam's WS STT (2026-07): audio rides as a
            # TOP-LEVEL `audio` object whose base64 `data` is a WAV (the `encoding`
            # field is a WAV-only enum), and `{"type": "flush"}` triggers the
            # transcript. Model/language ride the URL query — there is no config
            # frame. Non-WAV audio can't use this path, so degrade to batch.
            if fmt != "wav":
                raise ValueError("sarvam ws stt requires wav audio")
            await ws.send(
                json.dumps(
                    {
                        "audio": {
                            "data": base64.b64encode(raw).decode(),
                            "encoding": "audio/wav",
                            "sample_rate": _SARVAM_WS_SAMPLE_RATE,
                        }
                    }
                )
            )
            await ws.send(json.dumps({"type": "flush"}))
            while True:
                frame = await asyncio.wait_for(ws.recv(), timeout=self._timeout_s)
                msg = json.loads(frame)
                mtype = msg.get("type")
                if mtype == "data":
                    # One utterance + flush → this transcript is the final. (Interim
                    # "data" frames from a chunked stream would be partials with
                    # is_final=False; unused on this single-shot path.)
                    yield self._result_from_ws(request, msg.get("data", {}), is_final=True)
                    return
                if mtype == "error":
                    raise ValueError(f"sarvam stt ws error: {msg.get('data')}")

    def _result_from_ws(
        self, request: TranscribeRequest, data: dict, *, is_final: bool
    ) -> TranscriptResult:
        text = (data.get("transcript") or data.get("text") or "").strip()
        lang = _SARVAM_LANG.get(str(data.get("language_code", "")).lower(), Language.other)
        if request.language_hint and lang is Language.other:
            lang = request.language_hint
        confidence = data.get("language_probability")
        return TranscriptResult(
            session_id=request.session_id,
            text=text,
            is_final=is_final,
            language_id=lang,
            language_confidence=confidence if confidence is not None else 0.5,
            speech_duration_ms=max(120, len(text) * 60),
            word_confidence=0.9,  # Sarvam's WS response has no per-word score
        )

    async def warm(self) -> None:
        await warm_connection(self._client, self._url)

    async def aclose(self) -> None:
        await self._client.aclose()


# Deepgram realtime STT (nova-3) — protocol verified live (2026-07): raw linear16
# PCM is streamed as binary WS frames to `/v1/listen`; with `interim_results=true`
# Deepgram emits `is_final:false` partials as audio streams in, then an
# `is_final:true` final; `{"type":"CloseStream"}` flushes and a trailing
# `{"type":"Metadata"}` marks end-of-stream.
_DEEPGRAM_WS_PATH = "/v1/listen"
# ~250 ms of 16 kHz 16-bit mono PCM per binary frame — small enough that Deepgram
# streams interim partials as audio arrives, large enough to avoid frame churn.
_DEEPGRAM_CHUNK_BYTES = 8000


class DeepgramSttProvider(SttProvider):
    """Live *streaming* STT via Deepgram (nova-3, ``/v1/listen`` WS) — the O-6
    true-partials path.

    Deepgram's edge here is genuine incremental partials: with
    ``interim_results=true`` it emits ``is_final=false`` transcripts while audio
    streams in, then an ``is_final=true`` final on flush (verified live 2026-07).

    - :meth:`transcribe` (batch) delegates straight to ``fallback``. Deepgram's
      prerecorded REST adds nothing over the base batch provider already wired
      (Sarvam / Whisper / stub), so the ``/v1/transcribe`` chunk-mode path is
      byte-for-byte unchanged when this provider is in front.
    - :meth:`stream_transcribe` opens the Deepgram WS, streams the utterance's raw
      linear16 PCM (extracted from the WAV via the stdlib :mod:`wave` module) as
      binary frames, sends ``{"type":"CloseStream"}``, and yields a non-final
      ``TranscriptResult`` per interim + a final one for the ``is_final`` result.

    Degrade chain: any WS error / non-WAV / empty audio → :meth:`transcribe`
    (== ``fallback.transcribe``), which itself cascades through its own fallback to
    the deterministic stub — a live turn is never dropped and never raises.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "nova-3",
        base_url: str = "https://api.deepgram.com",
        fallback: SttProvider,
        timeout_s: float = 20.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._fallback = fallback
        self._timeout_s = timeout_s
        self._warned = False
        self._ws_warned = False
        # https:// → wss:// on the same host; encoding/sample_rate/channels are
        # appended per-utterance from the decoded WAV in _build_ws_url.
        self._ws_base = (
            base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        )

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        # Batch path delegates to the wrapped provider: Deepgram's value is
        # streaming partials, and the fallback already transcribes buffered audio.
        # Keeping this a pure delegate is what makes /v1/transcribe unchanged.
        return await self._fallback.transcribe(request)

    async def stream_transcribe(
        self, request: TranscribeRequest
    ) -> AsyncIterator[TranscriptResult]:
        """Stream Deepgram interims + a final over ``/v1/listen``; on any error
        (or non-WAV / empty / type-to-talk audio) degrade to the batch path.

        Degradation reuses the base default (:meth:`transcribe`, which here is
        ``fallback.transcribe`` → its own fallback → stub), so a live turn is never
        dropped and the streaming path never raises.
        """
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, binascii.Error):
            raw = b""

        # Type-to-talk sentinel or empty audio → skip the WS entirely.
        if raw and not raw.startswith(_TEXT_SENTINEL):
            produced_final = False
            try:
                async for result in self._stream_ws(request, raw):
                    produced_final = produced_final or result.is_final
                    yield result
            except Exception:  # noqa: BLE001 — never raise on the live path; degrade
                if not self._ws_warned:
                    self._ws_warned = True
                    logger.warning(
                        "deepgram_stt_ws_failed; degrading to fallback batch",
                        extra={"extra_fields": {"session_id": request.session_id}},
                        exc_info=True,
                    )
            else:
                if produced_final:
                    return
                # WS closed without a final transcript → fall through to the batch
                # path for an authoritative result (interims already delivered).
                logger.warning(
                    "deepgram_stt_ws_no_final; degrading to fallback batch",
                    extra={"extra_fields": {"session_id": request.session_id}},
                )

        # Batch path (base default): one final from transcribe() == fallback → stub.
        async for result in SttProvider.stream_transcribe(self, request):
            yield result

    async def _stream_ws(
        self, request: TranscribeRequest, raw: bytes
    ) -> AsyncIterator[TranscriptResult]:
        """Transcribe over Deepgram's realtime ``/v1/listen`` (protocol verified live,
        2026-07): stream the utterance's raw linear16 PCM as binary WS frames, send
        ``{"type":"CloseStream"}`` to flush, and read ``{"type":"Results",...}``
        frames — ``is_final:false`` are interim partials, ``is_final:true`` is the
        final, and a trailing ``{"type":"Metadata"}`` ends the stream.

        Errors before any transcript raise (so the caller degrades to batch); once
        results have been delivered we only ever ``return`` — never re-raise — so the
        caller doesn't double-yield a fallback final on top of a real one.
        """
        from websockets.asyncio.client import connect as ws_connect
        from websockets.exceptions import ConnectionClosed

        pcm, sample_rate, channels = self._extract_pcm(raw)
        async with ws_connect(
            self._build_ws_url(sample_rate, channels),
            additional_headers={"Authorization": f"Token {self._api_key}"},
            open_timeout=self._timeout_s,
            close_timeout=2.0,
        ) as ws:
            for i in range(0, len(pcm), _DEEPGRAM_CHUNK_BYTES):
                await ws.send(pcm[i : i + _DEEPGRAM_CHUNK_BYTES])
            # Flush: tell Deepgram no more audio — it finalizes and sends Metadata.
            await ws.send(json.dumps({"type": "CloseStream"}))
            yielded = False
            while True:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=self._timeout_s)
                except ConnectionClosed:
                    return  # socket closed after flush → stop cleanly (no raise)
                msg = json.loads(frame)
                mtype = msg.get("type")
                if mtype == "Results":
                    alts = (msg.get("channel") or {}).get("alternatives") or []
                    text = (alts[0].get("transcript") if alts else "") or ""
                    if not text.strip():
                        continue  # empty interim (silence) → don't emit a frame
                    yielded = True
                    yield self._result_from_ws(request, msg, is_final=bool(msg.get("is_final")))
                elif mtype == "Metadata":
                    return  # end-of-stream marker after CloseStream
                elif mtype in ("Error", "error"):
                    # Pre-transcript error → raise so we degrade; post-transcript →
                    # stop (results already delivered; don't trigger a double final).
                    if not yielded:
                        raise ValueError(f"deepgram stt ws error: {msg}")
                    return

    @staticmethod
    def _extract_pcm(raw: bytes) -> tuple[bytes, int, int]:
        """Extract raw linear16 PCM + sample rate + channel count from a WAV.

        Deepgram's realtime endpoint ingests raw ``linear16`` (not a WAV container),
        so we strip the RIFF header via the stdlib :mod:`wave` module and forward the
        true sample rate / channel count in the URL. Non-WAV or non-16-bit audio
        raises so the caller degrades to the batch fallback.
        """
        import io
        import wave

        if raw[:4] != b"RIFF":
            raise ValueError("deepgram ws stt requires wav audio")
        try:
            with wave.open(io.BytesIO(raw), "rb") as wf:
                if wf.getsampwidth() != 2:
                    raise ValueError("deepgram ws stt requires 16-bit pcm wav")
                return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels()
        except wave.Error as exc:
            raise ValueError(f"deepgram ws stt wav parse failed: {exc}") from exc

    def _build_ws_url(self, sample_rate: int, channels: int) -> str:
        return (
            f"{self._ws_base}{_DEEPGRAM_WS_PATH}"
            f"?model={self._model}&encoding=linear16&sample_rate={sample_rate}"
            f"&channels={channels}&interim_results=true&punctuate=true"
        )

    def _result_from_ws(
        self, request: TranscribeRequest, msg: dict, *, is_final: bool
    ) -> TranscriptResult:
        alts = (msg.get("channel") or {}).get("alternatives") or []
        alt = alts[0] if alts else {}
        text = (alt.get("transcript") or "").strip()
        confidence = alt.get("confidence")
        conf = float(confidence) if confidence is not None else 0.9
        duration = msg.get("duration")
        duration_ms = int(float(duration) * 1000) if duration else 0
        # nova-3's base response carries no language id, so carry the hint through
        # (English-centric model → default en). The authoritative language id +
        # escalation confidence still come from the batch fallback, not partials.
        lang = request.language_hint or Language.en
        return TranscriptResult(
            session_id=request.session_id,
            text=text,
            is_final=is_final,
            language_id=lang,
            language_confidence=conf,
            speech_duration_ms=duration_ms or max(120, len(text) * 60),
            word_confidence=conf,  # Deepgram alternative confidence (per-utterance)
        )

    async def warm(self) -> None:
        # No own client; warm the wrapped batch provider (the /v1/transcribe path).
        await self._fallback.warm()

    async def aclose(self) -> None:
        # Own no client — but close the wrapped provider's (Sarvam/Groq httpx).
        aclose = getattr(self._fallback, "aclose", None)
        if aclose is not None:
            await aclose()


class WhisperSageMakerProvider(SttProvider):
    """Real provider — invokes a SageMaker Whisper endpoint. Not wired in MVP boilerplate."""

    def __init__(self, endpoint_name: str, region: str) -> None:
        self.endpoint_name = endpoint_name
        self.region = region

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:  # pragma: no cover
        raise NotImplementedError(
            "Wire boto3 sagemaker-runtime invoke_endpoint here; return TranscriptResult."
        )
