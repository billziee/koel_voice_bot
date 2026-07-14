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
    # "stub"   → deterministic heuristic generator (CI / offline demo)
    # "groq"   → live Groq inference (OpenAI-compatible chat completions, US-hosted)
    # "sarvam" → live Sarvam inference (India-hosted; O-7 Track A residency path)
    nlp_provider: str = "stub"

    # --- Groq (https://console.groq.com) ---
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Language-routed models (latency): English uses the fast 8b-instant; Hindi/
    # Hinglish keep the stronger 70b because 8b is weaker at code-mixed JSON +
    # grounding. `groq_model` is the default/quality model used when unrouted.
    groq_model: str = "llama-3.3-70b-versatile"
    groq_model_quality: str = "llama-3.3-70b-versatile"  # hi / hinglish
    groq_model_fast: str = "llama-3.1-8b-instant"  # en
    # Script for Hinglish replies: "devanagari" (for NVIDIA Magpie hi-IN, which
    # needs Devanagari) or "latin" (romanized — for Orpheus/English-only TTS setups
    # that can't pronounce Devanagari). Default "latin": Hinglish is voiced by Orpheus
    # (only pure-Hindi "hi" bots route to Magpie, and those reply in Devanagari via
    # the "hi" script directive, not this Hinglish setting).
    hinglish_script: str = "latin"
    # Low temperature: keep turns grounded and on-script (PRD §12 anti-hallucination).
    groq_temperature: float = 0.3
    # Spoken turns are 1-3 sentences; a tight cap ends generation sooner (latency).
    groq_max_tokens: int = 160
    groq_timeout_s: float = 8.0

    # --- Sarvam (O-7 Track A: India-hosted NLP for DPDP/RBI residency) ---
    # Sarvam's chat API is OpenAI-compatible in body (messages + JSON mode), so
    # SarvamNlpProvider reuses all of GroqNlpProvider's prompt/parse/stream logic —
    # only the endpoint + auth header differ. Empty key ⇒ never selected.
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_model: str = "sarvam-m"

    @property
    def sarvam_nlp_enabled(self) -> bool:
        return self.nlp_provider.lower() == "sarvam" and bool(self.sarvam_api_key)

    # --- RAG embeddings (Groq has NO embedding model) ---
    # "hash"      → deterministic stub (CI/offline; semantically weak)
    # "fastembed" → local ONNX CPU embedder (O-4; no US round-trip). Needs the
    #               'local-embed' extra + a one-off KB re-embed (scripts/reembed_kb.py)
    # "nvidia"    → NVIDIA NIM multilingual embedder (handles Hindi/Hinglish)
    # "bedrock"   → AWS Bedrock Titan v2 in ap-south-1 (in-region; stub for now)
    nlp_embedder: str = "hash"
    # Local embedder model (O-4); bge-m3 is 1024-d multilingual → matches EMBEDDING_DIM.
    fastembed_model: str = "BAAI/bge-m3"
    nvidia_api_key: str | None = None  # shared KOEL_NVIDIA_API_KEY (also used by Magpie TTS)
    nvidia_embed_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_embed_model: str = "nvidia/nv-embedqa-e5-v5"  # 1024-d, matches EMBEDDING_DIM
    nvidia_embed_timeout_s: float = 20.0
    # Retrieval tuning (per-bot overrides land with the Alembic migration).
    # top_k=2 keeps the grounded prompt short (fewer input tokens → faster TTFT).
    rag_top_k: int = 2
    rag_score_threshold: float = 0.0  # drop chunks below this cosine score; tune ~0.3 for nvidia

    @property
    def groq_enabled(self) -> bool:
        return self.nlp_provider.lower() == "groq" and bool(self.groq_api_key)


@lru_cache
def get_nlp_settings() -> NlpSettings:
    return NlpSettings()
