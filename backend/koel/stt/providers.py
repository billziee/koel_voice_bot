"""STT provider interface + stub implementation.

``WhisperSageMakerProvider`` is intentionally a stub: the real implementation
invokes a SageMaker Whisper endpoint. The interface is what the rest of the
system is built against, so swapping the body is a config change, not a rewrite.
"""

from __future__ import annotations

import base64
import logging
import math
from abc import ABC, abstractmethod

import httpx

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


class SttProvider(ABC):
    @abstractmethod
    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult: ...


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
        except (ValueError, base64.binascii.Error):
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

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:
        try:
            raw = base64.b64decode(request.audio_b64) if request.audio_b64 else b""
        except (ValueError, base64.binascii.Error):
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
                    **({"language": request.language_hint.value}
                       if request.language_hint and request.language_hint != Language.hinglish else {}),
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
        lang = _WHISPER_LANG.get(str(data.get("language", "")).lower(), Language.other)
        if request.language_hint and lang is Language.other:
            lang = request.language_hint
        duration_ms = int(float(data.get("duration", 0)) * 1000)
        return TranscriptResult(
            session_id=request.session_id,
            text=text,
            is_final=request.is_final,
            language_id=lang,
            language_confidence=0.95,
            speech_duration_ms=duration_ms or max(120, len(text) * 60),
            word_confidence=self._confidence(data.get("segments", [])),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class WhisperSageMakerProvider(SttProvider):
    """Real provider — invokes a SageMaker Whisper endpoint. Not wired in MVP boilerplate."""

    def __init__(self, endpoint_name: str, region: str) -> None:
        self.endpoint_name = endpoint_name
        self.region = region

    async def transcribe(self, request: TranscribeRequest) -> TranscriptResult:  # pragma: no cover
        raise NotImplementedError(
            "Wire boto3 sagemaker-runtime invoke_endpoint here; return TranscriptResult."
        )
