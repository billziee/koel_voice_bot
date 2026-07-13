"""STT_svc request/response contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from koel.common.schemas import Language


class TranscribeRequest(BaseModel):
    session_id: str
    # Base64-encoded PCM/Opus audio chunk. Streaming clients send sequential
    # chunks; ``is_final`` marks end-of-utterance so the partial becomes final.
    audio_b64: str = Field(description="Base64-encoded audio chunk")
    sample_rate: int = 16000
    is_final: bool = False
    language_hint: Language | None = None
    # Container format of the audio (wav/webm/m4a/mp3/ogg/flac). Whisper infers
    # the codec from the filename extension, so this must match the bytes.
    audio_format: str = "wav"
    # BYOK (Phase 3): id of the org's custom STT ModelConnection to transcribe on.
    # STT_svc resolves it server-side (decrypts the key) and builds a provider by
    # its ``provider`` label (deepgram / sarvam); None ⇒ the service-default
    # provider. Only the id crosses the wire. Defaulted so existing serialized
    # request bodies stay valid.
    stt_connection_id: str | None = None


class TranscriptResult(BaseModel):
    session_id: str
    text: str
    is_final: bool
    # PRD §06 Graceful Language Fallback: every utterance carries a language id
    # + confidence so VB_Svc can escalate on unsupported languages.
    language_id: Language | None = None
    language_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    # Speech duration in ms — feeds Gate 2 backchannel classification (PRD §05).
    speech_duration_ms: int = 0
    word_confidence: float = Field(ge=0.0, le=1.0, default=1.0)
