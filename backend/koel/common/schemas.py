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
    # A detected language outside the supported set (Marathi, Bengali, Tamil…).
    # Only ever a *detected* id — never an operating language. Triggers the
    # graceful fallback + escalation in the orchestrator (PRD §06).
    other = "other"


class Tone(str, Enum):
    """Prosody tone tags (PRD §06 Tone & Prosody Control)."""

    cheerful = "cheerful"
    empathetic = "empathetic"
    firm = "firm"
    neutral = "neutral"
