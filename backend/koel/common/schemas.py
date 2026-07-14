"""Shared response schemas used across services."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    ok = "ok"
    degraded = "degraded"
    down = "down"


class HealthResponse(BaseModel):
    service: str
    status: HealthStatus = HealthStatus.ok
    version: str
    environment: str
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Readiness dependency probes: name → reachable? (P)
    dependencies: dict[str, bool] = Field(default_factory=dict)


class Language(str, Enum):
    """Languages supported at launch (PRD §01)."""

    en = "en"  # English
    hi = "hi"  # Hindi
    hinglish = "hinglish"  # code-mixed Hindi/English
    # Additional Indian languages via Sarvam Bulbul TTS + Whisper STT (4.1).
    # Operating languages only when Sarvam is enabled; require per-language
    # operational-copy authoring + native QA before production use (see 4.1 note).
    ta = "ta"  # Tamil
    te = "te"  # Telugu
    mr = "mr"  # Marathi
    bn = "bn"  # Bengali
    # A detected language outside the supported set. Only ever a *detected* id —
    # never an operating language. Triggers the graceful fallback + escalation.
    other = "other"


# Operating languages the platform can serve end to end (STT pin + Sarvam TTS).
# Used by the orchestrator's unsupported-language fallback (PRD §06).
SUPPORTED_LANGUAGES: frozenset[Language] = frozenset(
    {Language.en, Language.hi, Language.hinglish, Language.ta, Language.te, Language.mr, Language.bn}
)


class Tone(str, Enum):
    """Prosody tone tags (PRD §06 Tone & Prosody Control)."""

    cheerful = "cheerful"
    empathetic = "empathetic"
    firm = "firm"
    neutral = "neutral"
