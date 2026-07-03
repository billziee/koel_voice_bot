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
    # Silence AFTER the bot's audio finishes before a re-prompt/close. 5s felt
    # impatient on real calls (fired while the caller was still thinking), so the
    # window is more forgiving; the clock only starts once playback truly ends
    # (client playback_done) — see ws/handler.
    no_input_reprompt_ms: int = 8000  # re-prompt after 8s of real silence
    no_input_close_ms: int = 8000  # close after a further 8s

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
    calling_hours_start: int = 9  # inclusive, hour (24h) in calling_hours_tz
    calling_hours_end: int = 21  # exclusive, hour (24h) in calling_hours_tz
    calling_hours_tz: str = "Asia/Kolkata"  # window is evaluated in this timezone

    # Language handling. Per-utterance STT language DETECTION is unreliable on
    # code-mixed audio, so the bot's configured language stays authoritative and we
    # don't switch the response language from it.
    adapt_language_to_caller: bool = False
    # Graceful unsupported-language fallback (PRD §06). Safe to keep on now that STT
    # is language-pinned: a supported-language caller is never mis-escalated. An
    # unsupported language, force-transcribed into the bot's language, comes back
    # with very LOW confidence over a real (sustained) utterance — that's the signal.
    escalate_on_unsupported_language: bool = True
    unsupported_confidence_floor: float = 0.35  # below this on a real utterance ⇒ unsupported
    unsupported_min_speech_ms: int = 800  # ignore blips/noise; require sustained speech

    # Telephony seam (F): when true, synthesized audio is transcoded to 8 kHz
    # µ-law for a PSTN leg. Off for browser calls (the only transport built now).
    telephony_mode: bool = False

    # Sentence-streaming TTS (latency): stream one audio chunk per sentence so the
    # bot starts speaking on the first sentence, not the whole clip. Reversible.
    stream_tts: bool = True


@lru_cache
def get_voicebot_settings() -> VoiceBotSettings:
    return VoiceBotSettings()
