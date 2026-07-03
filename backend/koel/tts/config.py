"""TTS_svc configuration.

Selects the synthesis backend (deterministic stub vs. live Groq Orpheus).
Groq's Orpheus is OpenAI-compatible (``/audio/speech``) but requires one-time
org-admin terms acceptance in the Groq console; until then the live provider
degrades to the stub. The API key is the shared ``KOEL_GROQ_API_KEY``.
"""

from __future__ import annotations

from functools import lru_cache

from koel.common.config import CommonSettings
from koel.tts.schemas import CANONICAL_SAMPLE_RATE


class TtsSettings(CommonSettings):
    # "stub" → sized silent PCM + ambient bed; "groq" → live Orpheus neural TTS.
    tts_provider: str = "stub"

    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_orpheus_model: str = "canopylabs/orpheus-v1-english"
    # Groq Orpheus voices: autumn, diana, hannah (female) · austin, daniel, troy (male).
    groq_voice: str = "autumn"
    groq_audio_format: str = "wav"  # wav decodes everywhere + carries duration
    groq_timeout_s: float = 30.0

    # --- NVIDIA Magpie (Hindi/multilingual neural TTS via NVCF gRPC/Riva) ---
    # Orpheus is English; for Hindi we route to NVIDIA Magpie which speaks hi-IN
    # natively. Set tts_hindi_provider=nvidia + KOEL_NVIDIA_API_KEY to enable.
    tts_hindi_provider: str = "default"  # "default" (use base provider) | "nvidia"
    # Languages routed to the Hindi provider (comma-separated Language values).
    tts_nvidia_languages: str = "hi,hinglish"
    nvidia_api_key: str | None = None
    nvidia_tts_uri: str = "grpc.nvcf.nvidia.com:443"
    nvidia_tts_function_id: str = "877104f7-e885-42b9-8de8-f6e4c6303969"  # magpie-tts-multilingual
    nvidia_tts_voice: str = "Magpie-Multilingual"
    # MUST equal CANONICAL_SAMPLE_RATE: the ambient mixer, sentence concat, and the
    # browser gapless scheduler all assume ONE rate. Magpie at 44.1k while the rest
    # ran at 24k made Hindi/Devanagari play ~1.84x fast → high-pitched gibberish.
    nvidia_tts_sample_rate: int = CANONICAL_SAMPLE_RATE
    nvidia_tts_timeout_s: float = 30.0

    @property
    def groq_enabled(self) -> bool:
        return self.tts_provider.lower() == "groq" and bool(self.groq_api_key)

    @property
    def nvidia_enabled(self) -> bool:
        return self.tts_hindi_provider.lower() == "nvidia" and bool(self.nvidia_api_key)

    @property
    def nvidia_language_set(self) -> set[str]:
        return {x.strip().lower() for x in self.tts_nvidia_languages.split(",") if x.strip()}


@lru_cache
def get_tts_settings() -> TtsSettings:
    return TtsSettings()
