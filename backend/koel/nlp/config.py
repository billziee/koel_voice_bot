"""NLP_svc configuration.

Selects the response-generation backend (deterministic stub vs. a live LLM)
and carries the Groq credentials/parameters. Everything is environment-driven
(``KOEL_`` prefix) so the same image runs in dev, CI, and demo without code
changes. The real key lives in ``backend/.env`` (gitignored), never in source.
"""

from __future__ import annotations

from functools import lru_cache

from koel.common.config import CommonSettings


class NlpSettings(CommonSettings):
    # --- Provider selection ---
    # "stub"  → deterministic heuristic generator (CI / offline demo)
    # "groq"  → live Groq inference (OpenAI-compatible chat completions)
    nlp_provider: str = "stub"

    # --- Groq (https://console.groq.com) ---
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    # Low temperature: keep turns grounded and on-script (PRD §12 anti-hallucination).
    groq_temperature: float = 0.3
    groq_max_tokens: int = 512
    groq_timeout_s: float = 8.0

    @property
    def groq_enabled(self) -> bool:
        return self.nlp_provider.lower() == "groq" and bool(self.groq_api_key)


@lru_cache
def get_nlp_settings() -> NlpSettings:
    return NlpSettings()
