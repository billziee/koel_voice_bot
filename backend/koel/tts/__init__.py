"""TTS_svc — text-to-speech (PRD §05/§07: Orpheus on SageMaker).

Maps the NLP tone tag to prosody parameters (pitch, rate, energy) per PRD §06,
and streams audio so first-byte latency stays under the budget. The orchestrator
can issue TTS_KILL mid-stream for barge-in (PRD §05).
"""
