"""STT_svc configuration.

Selects the transcription backend (deterministic stub vs. live Groq Whisper).
Groq's Whisper is OpenAI-compatible (``/audio/transcriptions``). The API key is
the shared ``KOEL_GROQ_API_KEY`` (gitignored ``.env``), never in source.
"""

from __future__ import annotations

from functools import lru_cache

from koel.common.config import CommonSettings


class SttSettings(CommonSettings):
    # "stub" → deterministic type-to-talk stub; "groq" → live Whisper.
    stt_provider: str = "stub"

    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_whisper_model: str = "whisper-large-v3-turbo"
    groq_timeout_s: float = 30.0

    # --- Sarvam AI Saaras (primary STT — Indian-language + code-mixed speech) ---
    # Set KOEL_SARVAM_API_KEY to enable. When set, Sarvam becomes the STT
    # provider and Groq Whisper (if enabled) becomes ITS fallback.
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_stt_model: str = "saaras:v3"
    sarvam_mode: str = "transcribe"
    sarvam_timeout_s: float = 20.0
    # Streaming STT seam (O-6): when True, SarvamSttProvider.stream_transcribe
    # attempts the /speech-to-text/ws path (partial + final transcripts) before
    # degrading to batch transcribe(). OFF by default — the seam is dormant until
    # the WS handler ingest is rewired against a live Sarvam WS STT endpoint.
    stream_stt: bool = False

    # --- Deepgram realtime STT (O-6 true-partials — nova-3) ---
    # Which provider backs the STREAMING seam (/v1/transcribe/stream). "sarvam"
    # (default) keeps the existing Sarvam WS path; "deepgram" wraps the base
    # provider with DeepgramSttProvider so stream_transcribe() upgrades to
    # Deepgram's genuine interim partials. The batch /v1/transcribe path is
    # unaffected either way. Deepgram only engages when its key is also set.
    stream_stt_provider: str = "sarvam"  # "sarvam" | "deepgram"
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    deepgram_base_url: str = "https://api.deepgram.com"

    @property
    def groq_enabled(self) -> bool:
        return self.stt_provider.lower() == "groq" and bool(self.groq_api_key)

    @property
    def sarvam_enabled(self) -> bool:
        return bool(self.sarvam_api_key)

    @property
    def deepgram_enabled(self) -> bool:
        return bool(self.deepgram_api_key)


@lru_cache
def get_stt_settings() -> SttSettings:
    return SttSettings()
