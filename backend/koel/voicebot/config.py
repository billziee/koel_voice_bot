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
    # Min-words gate: a lone non-backchannel word is usually STT noise (horn,
    # cough) — require ≥2 words, or a long-held single word, to kill TTS.
    min_interrupt_words: int = 2
    single_word_interrupt_ms: int = 1200

    # Filler audio (PRD §05 Latency Masking).
    filler_trigger_ms: int = 300  # inject filler if NLP first token exceeds this

    # Semantic end-of-turn gate (smart-turn v3.2 ONNX; docs/PIPECAT_UPGRADE_PLAN
    # Phase 4). When the utterance audio says the speaker paused mid-thought,
    # hold the transcript up to smart_turn_hold_ms and merge the follow-up
    # instead of answering half a sentence. Requires the `smart-turn` extra +
    # scripts/fetch_smart_turn_model.sh; degrades to off if either is missing.
    smart_turn_enabled: bool = False
    smart_turn_model_path: str = "models/smart-turn-v3.2-cpu.onnx"
    smart_turn_threshold: float = 0.5
    smart_turn_hold_ms: int = 1500

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
    # Declare the peer dead if no pong arrives within this window. Must clear a
    # bot turn's worst-case audio delivery time: the ping/pong shares the same
    # ordered WS connection as audio_chunk frames, so a large turn (e.g. a
    # multi-sentence Hindi reply) can queue the ping behind it — a live,
    # actively-streaming connection is not a dead one. Measured: a 3-sentence
    # Sarvam TTS greeting alone pushed ping delivery out to ~14s; 12s (~2
    # missed 5s pings) was tuned for near-instant ping delivery and false-
    # positived on exactly the connections doing real work.
    heartbeat_timeout_seconds: int = 45

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

    # Sarvam AI: transliteration (derives Hinglish operational copy from its
    # Devanagari source at startup — see pipeline/localization.py) and chat
    # completions (post-call CRM summarizer). Shares KOEL_SARVAM_API_KEY with
    # the TTS/STT Sarvam integrations.
    sarvam_api_key: str | None = None
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_translit_timeout_s: float = 15.0
    sarvam_summary_model: str = "sarvam-30b"
    sarvam_summary_timeout_s: float = 40.0

    @property
    def sarvam_enabled(self) -> bool:
        return bool(self.sarvam_api_key)


@lru_cache
def get_voicebot_settings() -> VoiceBotSettings:
    return VoiceBotSettings()
