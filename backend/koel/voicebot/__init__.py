"""VoiceBot_svc (VB_Svc) — the orchestration brain (PRD §05).

Owns session lifecycle, routes audio → STT → NLP → TTS, holds Redis-backed
session state, and enforces conversation-resilience rules: dual-gated barge-in,
latency-masking fillers, silence/timeout handling, and 15s WebSocket recovery.
"""
