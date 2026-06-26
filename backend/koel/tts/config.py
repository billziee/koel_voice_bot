"""TTS_svc configuration.

Selects the synthesis backend (deterministic stub vs. live Groq Orpheus).
Groq's Orpheus is OpenAI-compatible (``/audio/speech``) but requires one-time
org-admin terms acceptance in the Groq console; until then the live provider
degrades to the stub. The API key is the shared ``KOEL_GROQ_API_KEY``.
"""

from __future__ import annotations

from functools import lru_cache

from koel.common.config import CommonSettings


class TtsSettings(CommonSettings):
    # "stub" → sized silent PCM + ambient bed; "groq" → live Orpheus neural TTS.
    tts_provider: str = "stub"

    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_orpheus_model: str = "canopylabs/orpheus-v1-english"
    groq_voice: str = "tara"
    groq_audio_format: str = "wav"  # wav decodes everywhere + carries duration
    groq_timeout_s: float = 30.0

    @property
    def groq_enabled(self) -> bool:
        return self.tts_provider.lower() == "groq" and bool(self.groq_api_key)


@lru_cache
def get_tts_settings() -> TtsSettings:
    return TtsSettings()
