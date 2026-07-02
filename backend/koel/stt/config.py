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

    @property
    def groq_enabled(self) -> bool:
        return self.stt_provider.lower() == "groq" and bool(self.groq_api_key)


@lru_cache
def get_stt_settings() -> SttSettings:
    return SttSettings()
