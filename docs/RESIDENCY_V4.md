# Data Residency v4 — accepted risk ledger

> Supersedes [RESIDENCY_V3.md](./RESIDENCY_V3.md). Tracks where customer audio/text
> leaves India, and the v4 changes to that ledger.

## US-egress ledger (v1 → v2 → v3 → v4)

| Provider (path) | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Groq (NLP + Whisper STT) | NLP only (real) | NLP + STT | unchanged (US) | unchanged (US) |
| NVIDIA (Magpie TTS + embeddings) | — | US | embeddings **optionally removed** by O-4 local FastEmbed | unchanged (Magpie TTS still US) |
| Sarvam (STT/TTS) | — | **India-hosted** (Hindi/Hinglish) | unchanged; WS STT protocol verified | unchanged |
| Deepgram (streaming STT) | — | — | **NEW US egress** (interim partials) — added to the ledger | unchanged (still opt-in per-bot `stream_stt`, still US) |
| RAG embeddings | hash (local) | NVIDIA (US) | local FastEmbed removes this US hop when enabled (O-4) | **Bedrock Titan (`BedrockTitanEmbedder`) is now a real boto3 implementation** (ap-south-1) — previously raised `NotImplementedError`. Closes this US hop *when selected*, but selection is still opt-in (`KOEL_NLP_EMBEDDER=bedrock`, default remains hash/NVIDIA) and needs a Bedrock model-access grant in `ap-south-1` before it can actually be used |
| **SageMaker (Whisper STT + Orpheus TTS, in-region)** | — | — | — | **NEW row.** `WhisperSageMakerProvider` / `OrpheusSageMakerProvider` are real boto3 call-shape implementations now, but **no endpoint is deployed** — Terraform for both exists (`infra/terraform/main.tf`) but was only validated/`fmt`'d, never `apply`'d. Closes zero egress line items today; removes future engineering effort once endpoints exist |

## v4 posture

- **No egress line item actually closes in v4.** Status is unchanged from v3: NLP
  (Groq) and English STT/TTS (Groq) still egress to the US on the default path;
  Deepgram remains an opt-in US hop for streaming partials; Sarvam's India-hosted
  Hindi/Hinglish coverage is unchanged.
- **Feasibility progress, not compliance progress.** `BedrockTitanEmbedder` went
  from a stub (`NotImplementedError`, v3) to a real, degrade-safe implementation
  against `bedrock-runtime` in `ap-south-1`. This only reduces US egress if an
  operator flips `KOEL_NLP_EMBEDDER=bedrock` *and* has been granted Bedrock model
  access — the shipped default embedder config is unchanged, so this is a lever
  now available, not a fix already applied to the live deployment.
- Same story for SageMaker Whisper/Orpheus: real request/response code exists
  against zero deployed endpoints. Terraform is written and `validate`/`fmt`-clean
  but deliberately never `apply`'d (inventing a fake endpoint ARN would be worse
  than an honest gap — see the file's own header comment). This is inventory for
  a future in-region rollout, not a residency change today.
- **Residency-adjacent, not US-egress:** telephony calls (Twilio Media Streams)
  are, for the first time, durably persisted at all — `TelephonyCallSession` now
  writes a `CallLog` row via `persist_call()` on every call (previously a
  telephony call left no DB row whatsoever), and both bot **and** customer audio
  are now recorded to S3 for **both** transports (previously only the bot's side
  was ever captured, on either transport). This expands the *volume* of customer
  PII durably captured, not the US-egress surface — the recording bucket and
  Bedrock region both default to `ap-south-1` (`s3_region`, `aws_region`) — but it
  raises the stakes on confirming the deployed bucket/DB region actually matches
  those defaults before treating any of this as compliant by default.
- In-region inference (O-7) — SageMaker Whisper/Orpheus + Bedrock in `ap-south-1`
  — remains the documented path to eliminate US egress; still legal-gated (see
  [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md)). PII redaction before every US
  egress ([SECURITY_V3.md](./SECURITY_V3.md)) remains the interim control.
