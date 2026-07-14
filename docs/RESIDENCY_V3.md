# Data Residency v3 — accepted risk ledger

> Supersedes [RESIDENCY.md](./RESIDENCY.md). Tracks where customer audio/text
> leaves India, and the v3 changes to that ledger.

## US-egress ledger (v1 → v2 → v3)

| Provider (path) | v1 | v2 | v3 |
|---|---|---|---|
| Groq (NLP + Whisper STT) | NLP only (real) | NLP + STT | unchanged (US) |
| NVIDIA (Magpie TTS + embeddings) | — | US | embeddings **optionally removed** by O-4 local FastEmbed |
| Sarvam (STT/TTS) | — | **India-hosted** (Hindi/Hinglish) | unchanged; WS STT protocol verified |
| **Deepgram (streaming STT)** | — | — | **NEW US egress** (interim partials) — added to the ledger |
| RAG embeddings | hash (local) | NVIDIA (US) | **local FastEmbed removes this US hop when enabled (O-4)** |

## v3 posture
- Net new residency exposure in v3 = **Deepgram** (US) for the streaming STT path;
  it's opt-in per bot (`stream_stt`), and the India-hosted Sarvam batch path
  remains the default for Hindi/Hinglish.
- O-4 (local embedder) is a **partial mitigation** — it removes the NVIDIA
  embedding round-trip entirely when enabled.
- **In-region inference (O-7)** — SageMaker Whisper/Orpheus + Bedrock in
  `ap-south-1` — remains the documented path to eliminate US egress; still
  legal-gated (see [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md)). PII redaction
  before every US egress ([SECURITY_V3.md](./SECURITY_V3.md)) remains the interim
  control.
