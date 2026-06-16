"""KOEL Voice Bot backend monorepo.

Packages:
    koel.common   — shared boilerplate (config, logging, telemetry, auth, errors, app factory)
    koel.db       — SQLAlchemy models + schema for sessions, call logs, transcripts, CRM, RAG
    koel.voicebot — VoiceBot_svc orchestrator (WebSocket session router + pipeline)
    koel.stt      — STT_svc (Whisper provider interface)
    koel.nlp      — NLP_svc (Bedrock + RAG provider interface)
    koel.tts      — TTS_svc (Orpheus provider interface)
    koel.crm      — Dummy CRM service
"""

__version__ = "1.2.0"
