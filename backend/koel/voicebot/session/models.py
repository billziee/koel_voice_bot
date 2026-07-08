"""Session state held in Redis (PRD §08: no in-memory state in app servers).

Everything needed to resume a call after a reconnect lives here, so any VB_Svc
instance can pick up a re-bound socket — horizontal scaling without sticky
sessions (PRD §08 Session Management).
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from koel.common.schemas import Language, Tone


class CallStatus(str, enum.Enum):
    connecting = "connecting"
    bot_speaking = "bot_speaking"
    listening = "listening"
    processing = "processing"
    escalating = "escalating"
    reconnecting = "reconnecting"  # within the 15s grace window
    ended = "ended"


def _now() -> datetime:
    return datetime.now(UTC)


class Turn(BaseModel):
    index: int
    speaker: str  # "bot" | "customer"
    text: str
    language: Language | None = None
    intent: str | None = None
    tone: Tone | None = None
    interrupted: bool = False  # bot turn cut short by barge-in (PRD §05)
    confidence: float | None = None
    at: datetime = Field(default_factory=_now)


class SessionState(BaseModel):
    """The full, serialisable call state. Persisted to Redis on every mutation."""

    session_id: str
    bot_id: str
    # The authenticated user who started the session. The WebSocket upgrade must
    # present a token whose sub matches, so one operator cannot attach to or
    # eavesdrop another's live call (authZ, not just authN).
    owner_sub: str | None = None
    customer_id: str | None = None
    org_id: str | None = None  # tenant scope carried onto the CallLog (AF)
    use_case: str
    language: Language = Language.hinglish
    rag_enabled: bool = True
    rag_index: str | None = None
    # Per-bot RAG tuning overrides (W); None → NLP_svc defaults.
    rag_top_k: int | None = None
    rag_score_threshold: float | None = None
    # Per-bot NLP overrides (roadmap 5.4); None → NLP_svc defaults. prompt_preamble
    # is added to (never replaces) the hardened system prompt.
    llm_model: str | None = None
    llm_temperature: float | None = None
    prompt_preamble: str | None = None
    default_tone: Tone = Tone.neutral
    # Persona (G): carried from bot config into every SynthesizeRequest.
    voice_gender: str = "female"
    speaking_rate: float = 1.0
    ambient_profile: str = "callfloor"
    greeting_template: str = ""  # from bot config; rendered with CRM fields at greet
    # Per-bot backchannel overrides (PRD §05): {language: [words…]} merged on top
    # of the default dictionary so a bot instance can tune its Gate-2 vocabulary.
    backchannels: dict[str, list[str]] = Field(default_factory=dict)
    # Audit fields (PRD §11 / §14). recording_url is the intended S3 object;
    # trace_id stitches the call to its OTel trace for the latency dashboards.
    recording_url: str | None = None
    trace_id: str | None = None

    status: CallStatus = CallStatus.connecting
    # CRM fields seeded at session init (name, product, offer, dpd…).
    crm_context: dict = Field(default_factory=dict)
    transcript: list[Turn] = Field(default_factory=list)
    # Current script position so a reconnect re-anchors to the last confirmed turn.
    last_confirmed_turn_index: int = -1
    # Rolling window of the customer's recent per-turn sentiment (roadmap 4.3),
    # newest last, capped by the orchestrator. A sustained negative trend softens
    # the bot's tone on the next turn.
    recent_sentiments: list[str] = Field(default_factory=list)

    # Resilience bookkeeping.
    no_input_strikes: int = 0  # 0 → re-prompt next, 1 → close next (PRD §05)
    escalated: bool = False
    outcome: str = "in_progress"
    consent_captured: bool = False  # DPDP / IRDAI consent (PRD §11)
    consent_at: datetime | None = None  # timestamp consent was captured (PRD §11)

    started_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def add_turn(self, turn: Turn) -> None:
        self.transcript.append(turn)
        self.updated_at = _now()

    def next_turn_index(self) -> int:
        return len(self.transcript)

    def recent_history(self, limit: int = 8) -> list[dict]:
        return [{"speaker": t.speaker, "text": t.text} for t in self.transcript[-limit:]]
