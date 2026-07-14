"""WebSocket message protocol between the React frontend and VB_Svc.

Persistent full-duplex connection (PRD §05/§07). Messages are JSON envelopes
with a ``type`` discriminator. Audio is base64 in/out for transport simplicity
in the MVP boilerplate.

Client → Server:
  audio_chunk        — streamed mic audio (with is_final on end-of-utterance)
  barge_in_candidate — Gate 1 VAD fired client-side while bot was speaking
  pong               — heartbeat reply
  end_call           — user hung up

Server → Client:
  call_state         — status badge transitions (PRD §09 Screen 3)
  transcript         — a completed turn (bot or customer) for the live transcript
  partial_transcript — interim (non-final) live STT of the customer's in-progress
                       utterance (O-6 streaming STT). BEST-EFFORT / NON-AUTHORITATIVE:
                       it may be revised or replaced by the final ``transcript`` and
                       must never drive a turn — purely a live-caption affordance.
  audio_chunk        — synthesized bot audio to play
  tts_kill           — flush the playback buffer NOW (barge-in confirmed, PRD §05)
  filler             — latency-masking filler is playing
  reconnected        — session re-bound after a drop (PRD §05)
  ping               — heartbeat
  error              — structured error
  call_ended         — final outcome
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, Field


class ClientMessageType(str, enum.Enum):
    audio_chunk = "audio_chunk"
    barge_in_candidate = "barge_in_candidate"
    pong = "pong"
    end_call = "end_call"
    playback_done = "playback_done"  # client audio queue drained — bot truly stopped speaking


class ServerMessageType(str, enum.Enum):
    call_state = "call_state"
    transcript = "transcript"
    # Interim, non-authoritative live STT partial (O-6 streaming). Carries at least
    # ``text`` and ``speaker`` in ``data``; the authoritative turn still arrives as
    # a ``transcript``. Ignored by transports without a live-caption surface (the
    # Twilio bridge drops it — a carrier has no representation for it).
    partial_transcript = "partial_transcript"
    audio_chunk = "audio_chunk"
    tts_kill = "tts_kill"
    filler = "filler"
    reconnected = "reconnected"
    ping = "ping"
    error = "error"
    call_ended = "call_ended"


class ClientMessage(BaseModel):
    type: ClientMessageType
    # audio_chunk fields
    audio_b64: str | None = None
    is_final: bool = False
    sample_rate: int = 16000
    # Container of the captured mic audio (webm/mp4/wav/…) so STT names the file
    # with the right extension for the codec (real mic sends webm/opus).
    audio_format: str = "wav"
    # Same utterance as raw s16le mono 16kHz PCM (from the browser's Silero VAD
    # worklet) — feeds the semantic end-of-turn gate with no server-side audio
    # decoding. Only present when the neural mic VAD is active.
    pcm16_b64: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ServerMessage(BaseModel):
    type: ServerMessageType
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, type_: ServerMessageType, **data: Any) -> ServerMessage:
        return cls(type=type_, data=data)
