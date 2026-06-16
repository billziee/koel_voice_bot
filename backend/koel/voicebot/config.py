"""VB_Svc settings — extends the shared config with orchestration tunables.

Every threshold here is a PRD number, surfaced as config so the resilience
behaviours (PRD §05) are tunable per deployment without code changes.
"""

from __future__ import annotations

from functools import lru_cache

from koel.common.config import CommonSettings


class VoiceBotSettings(CommonSettings):
    # Latency budget (PRD §01/§05/§14).
    turn_latency_budget_ms: int = 300

    # Barge-in (PRD §05 Dual-Gated Interruption Engine).
    backchannel_max_speech_ms: int = 300  # utterances shorter than this are ignored
    barge_in_stop_target_ms: int = 150  # confirmed interrupt → audible silence

    # Filler audio (PRD §05 Latency Masking).
    filler_trigger_ms: int = 300  # inject filler if NLP first token exceeds this

    # Silence / timeout rules (PRD §05 table).
    no_input_reprompt_ms: int = 5000  # re-prompt after 5s silence
    no_input_close_ms: int = 5000  # close after a further 5s

    # WebSocket lifecycle (PRD §05 / §08).
    session_grace_ttl_seconds: int = 15  # Redis grace on disconnect
    session_active_ttl_seconds: int = 3600  # active session TTL
    heartbeat_interval_seconds: int = 5  # ping/pong dead-connection detection
    # Declare the peer dead if no pong arrives within this window (~2 missed
    # pings). Detected liveness loss is routed to the same 15s grace path.
    heartbeat_timeout_seconds: int = 12

    # Language fallback (PRD §06): escalate below this STT language confidence
    # for an unsupported language.
    language_confidence_threshold: float = 0.6

    # RBI fair-practice calling-hours window (PRD §11). Off by default so demos
    # run any time; enable in production to refuse out-of-hours dials.
    enforce_calling_hours: bool = False
    calling_hours_start: int = 9  # inclusive, local hour (24h)
    calling_hours_end: int = 21  # exclusive, local hour (24h)


@lru_cache
def get_voicebot_settings() -> VoiceBotSettings:
    return VoiceBotSettings()
