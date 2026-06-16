"""TTS_svc contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from koel.common.schemas import Language, Tone


class Prosody(BaseModel):
    """Tone → prosody mapping (PRD §06 Tone & Prosody Control)."""

    pitch: float = Field(default=1.0, description="Relative pitch multiplier")
    rate: float = Field(default=1.0, description="Speaking rate multiplier")
    energy: float = Field(default=1.0, description="Loudness/energy multiplier")


# Default prosody per tone tag.
TONE_PROSODY: dict[Tone, Prosody] = {
    Tone.cheerful: Prosody(pitch=1.08, rate=1.05, energy=1.1),
    Tone.empathetic: Prosody(pitch=0.97, rate=0.92, energy=0.9),
    Tone.firm: Prosody(pitch=0.98, rate=0.98, energy=1.05),
    Tone.neutral: Prosody(pitch=1.0, rate=1.0, energy=1.0),
}


class SynthesizeRequest(BaseModel):
    session_id: str
    text: str
    language: Language = Language.hinglish
    tone: Tone = Tone.neutral
    voice_gender: str = "female"
    speaking_rate: float = 1.0  # bot-config override, multiplies tone rate
    # Stream chunks back vs. return a single clip. Streaming enables barge-in.
    stream: bool = True
    # Ambient audio layer (PRD §06): mix a low-level call-floor bed under the
    # speech so the line isn't dead-silent. Muted during escalation handoff.
    ambient: bool = False
    ambient_profile: str | None = None
    ambient_gain: float = 0.06  # relative loudness of the bed under speech


class SynthesisChunk(BaseModel):
    session_id: str
    seq: int
    audio_b64: str
    is_final: bool = False


class SynthesisResult(BaseModel):
    """Non-streaming response (used for short clips and the filler cache)."""

    session_id: str
    audio_b64: str
    duration_ms: int
    prosody: Prosody
