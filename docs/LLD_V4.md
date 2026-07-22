# KOEL Voice Bot — Low-Level Design v4

> Supersedes [LLD_V3.md](./LLD_V3.md) (→ [LLD_V2.md](./LLD_V2.md) →
> [LLD.md](./LLD.md)). Module-level view of the v4 codebase. Every table now
> carries **v1 / v2 / v3 / v4** columns. Companion: [HLD_V4.md](./HLD_V4.md).
> Authoritative cross-check for current state: [HANDOFF_V4_FINAL.md](./HANDOFF_V4_FINAL.md).

## What changed since v3 — module-level diff (v1 → v2 → v3 → v4)

| Area | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| **Pipeline modules** | `bargein, filler, silence, orchestrator` | + `endpointing, localization, transliteration, summarizer, whisper_features` | + `cost.py`, speculative-RAG hold in `orchestrator` (O-5), streaming turn `_stream_bot_turn` (O-2) | + **`specrag.py`** (`SpeculativeRagCoordinator` — O-5 extracted out of `orchestrator`/`CallSession` into a standalone, transport-reusable unit) + **`postcall.py`** (`dispatch_followup`, `summarize_and_annotate_crm` — extracted, shared by both transports) + `warm_transfer.py` gains **`end_twilio_call`** (REST hangup) alongside `start_warm_transfer` + `dialer_worker.py`/`outbound_dialer.py` (campaign dialer, now real origination) |
| **Telephony (new surface)** | — | — | `telephony/ingest.py` seam only (dormant) | **`telephony/session.py`** — `TelephonyCallSession` (the phone-transport analogue of `ws/handler.CallSession`: full turn-handling policy, no-input watchdog, escalation gating, call recording, persistence); `bridge.py` gains `on_started`/`on_outbound` hooks + `stopped` property + idle-frame watchdog (`frame_idle_timeout_s`); `ingest.py`'s `UtteranceCallback` widened to `(TranscriptResult, bytes)` (carries raw PCM16 for smart-turn analysis) and `TelephonyAudioIngest` gains `on_voice_activity` (RMS Gate 1); `bargein_gate.py`'s `TelephonyBargeInGate` now fully wired for both the buffered and streaming ingest paths |
| **WS layer** | `handler, protocol` | + `recording.py` | + `ws/streaming_stt.py` + `partial_transcript` server msg | `CallSession` unchanged in shape but now delegates post-call side effects to `postcall.py`; both `_handle_audio` (buffered) and `streaming_stt.py` (O-6) now buffer **customer** audio too, not just the bot's |
| **Call recording** | — | bot-audio only | bot-audio only | **bidirectional on both transports**: bot AND customer audio buffered + `concat_wavs`'d into one WAV (`tts/mixer.py` gains a public `pcm16_to_wav` helper); previously only the bot's half was ever recorded, on either transport |
| **Auth** | — | JWT login | unchanged | + **Google Sign-In** (`api/auth.py`: `POST /v1/auth/google`, allowlist model, ID-token verified against Google's JWKS) alongside password login; `User` gains `email`, `google_sub`, `auth_provider` |
| **Admin / multi-tenant** | — | — | Users/org registry (6.4), connections CRUD | + **org + user provisioning**: `api/users.py` (`POST /v1/users/invite`, `/accept-invite`, PATCH/deactivate/reactivate, reset-password, resend-invite) + `api/orgs.py` (org CRUD); `User` gains invite/reset fields (`invite_selector`, `invite_verifier_hash`, `invite_expires_at`, `invited_by`) |
| **Auto flow-builder** | — | — | flow editor UI (6.6, manual) | + `nlp/flow_gen.py::generate_flow` + `POST /v1/knowledge/generate-flow` — generates a conversation flow graph from RAG-indexed KB data; Bot Builder Step 4 (Knowledge & CRM) now wired to live data and reordered before the flow step (`ef3e8b5`) |
| **Outbound dialer** | — | — | — | **new**: `voicebot/campaigns.py` (`enqueue_dial`/`claim_due_attempts`/`record_dial_result`, `FOR UPDATE SKIP LOCKED` claim), `pipeline/dialer_worker.py` (background drain loop, calling-hours gate), `pipeline/outbound_dialer.py` (`originate_call` — real Twilio REST origination, httpx not the SDK); `db/models.py` gains `Campaign`/`DialAttempt`(+`DialAttemptStatus`) |
| **Escalation / monitor** | — | in-process only | in-process only | `EscalationHub`/`LiveMonitor` now **Redis pub/sub-backed** (`redis_client` optional ctor arg; `run_listener`/`_publish`/`_dispatch` in `escalation.py`, per-entry TTL + `smembers`-backed active set in `monitor.py`) for cross-worker fan-out; degrade to in-process when no Redis client is wired |
| **STT providers** | `Stub` only | + `GroqWhisper`, `SarvamStt`, SageMaker seam | + `DeepgramStreaming*` | seam → **real**: `WhisperSageMakerProvider` (`stt/providers.py:734`, boto3 `sagemaker-runtime`, lazy import) — code real, no endpoint deployed |
| **TTS providers** | stub | all real | unchanged | seam → **real**: `OrpheusSageMakerProvider` (`tts/providers.py:750`, same boto3 pattern) — code real, no endpoint deployed |
| **RAG embedder** | `HashEmbedder` | + NVIDIA, Bedrock seam | + `FastEmbedEmbedder` | seam → **real**: `BedrockTitanEmbedder` (`nlp/rag.py:160`, Bedrock Titan Embeddings V2, `ap-south-1`) — needs a Bedrock model-access grant to actually invoke |
| **Infra** | — | — | — | **new**: `infra/terraform/{main.tf,variables.tf}` — S3 (KMS+lifecycle), RDS Postgres (Multi-AZ), ElastiCache Redis (Multi-AZ, AUTH), least-privilege IAM (S3 + Bedrock-Titan-only invoke), ECS Fargate; written, never `apply`'d, deliberately no SageMaker endpoint resource; `render.yaml` reconciled with the live Render services |
| **Boot / production-readiness** | — | — | — | `koel/common/app_factory.py::warn_if_byok_secret_unset` + `koel/voicebot/main.py::warn_if_turnstile_unenforced` (startup warnings, not hard fails); `combined.py`'s boot sequence now **always** runs `alembic upgrade head`, gating only the expensive full seed behind an empty-DB check (previously migrations could be skipped) |
| **Bug-hunt hardening** | — | — | — | 4 batches merged: cross-tenant IDOR + high-sev correctness/security (`dd78305`), WS finalize/data-loss cluster (`7845574`), medium/low correctness + authz + FE fixes (`a20d3d1`), telephony frame-parse hardening — malformed carrier frames logged+skipped, never crash the call (`2dc262c`) |
| **Frontend** | monolith `App.tsx` | + mic/VAD | decomposed + `useLiveCall`, `useConnections`, flow editor | + `AdminView`/`AcceptInviteView` (org/user provisioning UI), FlowEditor now backed by generate-flow |
| **Config surface** | ~20 | ~40 | + streaming/Redis/embedder/budget/pool/cache/BYOK | + `telephony_no_input_reprompt_ms`/`telephony_no_input_close_ms`, `telephony_frame_idle_timeout_s`, `google_client_id`/`google_jwks_url`/`google_allowlist`, dialer calling-hours knobs, `twilio_default_bot_id` |
| **Tests** | 37 | 264 | 680 | **913** (1 skipped, 0 failing), 91.87% statement coverage vs. an 85% CI gate |

---

## 1. Module layout (v4 current)
Backend `koel/{common,db,stt,nlp,tts,crm,voicebot}` + `koel.combined`, unchanged
at the package level. New v4 files: `voicebot/telephony/session.py`
(`TelephonyCallSession`), `voicebot/pipeline/specrag.py`
(`SpeculativeRagCoordinator`), `voicebot/pipeline/postcall.py`
(`dispatch_followup`, `summarize_and_annotate_crm`), `voicebot/campaigns.py`,
`voicebot/pipeline/dialer_worker.py`, `voicebot/pipeline/outbound_dialer.py`,
`voicebot/api/users.py`, `voicebot/api/orgs.py`, `nlp/flow_gen.py`,
`infra/terraform/{main.tf,variables.tf}`. `telephony/ingest.py`,
`telephony/bridge.py`, `telephony/bargein_gate.py`, `pipeline/warm_transfer.py`,
`voicebot/escalation.py`, `voicebot/monitor.py` all existed pre-v4 as
seams/Phase-A pieces and are now fully wired end-to-end. Frontend unchanged
package layout, +`views/AdminView.tsx`, `views/AcceptInviteView.tsx`.

## 2. Shared core (`koel/common`) — v1 → v2 → v3 → v4
| Module | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| `http.py` | — | shared async client | unchanged | unchanged (reused by `outbound_dialer.py`/`warm_transfer.py`'s Twilio REST calls) |
| `ratelimit.py` | in-mem | in-mem | + `RedisRateLimiter` | unchanged; `_google_limit` (fail-closed namespace) added in `api/auth.py` |
| `resilience.py` | — | breaker + retry | reused by streaming fallbacks | unchanged |
| `text_guard.py` | — | PII redaction | reused per-sentence | unchanged |
| `config.py` | ~20 | ~40 | + streaming/redis/embedder/budget/pool/cache/BYOK | + telephony no-input/idle-timeout knobs, Google OAuth settings, dialer settings |
| `app_factory.py` | — | — | — | **new boot check**: `warn_if_byok_secret_unset` (warns once in production if `KOEL_SECRET_KEY` unset) |

## 3. Data model (`koel/db/models.py`) — v4 additions
v3: `Org`, `User` (base), `ModelConnection`, `CrmCustomer`, `BotConfig`,
`CallLog`, `TranscriptTurn`, `EscalationRequest`, `ConsentRecord`,
`KnowledgeChunk`, `ExportJob`, `InteractionLogRow`, `BotConfigAudit` (~13
tables). **v4 adds**: `Campaign` + `DialAttempt`/`DialAttemptStatus` (outbound
dialer queue, `FOR UPDATE SKIP LOCKED` claim on Postgres, plain windowed
select+update on SQLite/tests). `User` gains `email`, `google_sub`,
`auth_provider` (`"password"` | `"google"`), and the invite/reset-token
quartet `invite_selector`/`invite_verifier_hash`/`invite_expires_at`/
`invited_by` (split selector+verifier, shared by invite-accept and
admin-triggered password reset). ~21 tables total (per `HANDOFF_V4_FINAL.md`).

## 4. WebSocket / carrier protocol — v1 → v2 → v3 → v4
| | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| client (browser) | `audio_chunk, barge_in_candidate, pong, end_call` | + `playback_done` | continuous `audio_chunk` feeds the streaming session | unchanged |
| server (browser) | 8 types | same | + `partial_transcript` | unchanged |
| Twilio Media Streams | n/a | n/a | `start/media/dtmf/stop` parsed, seam-only | **live-wired**: `start` → greet + `on_started` hook (persists `twilio_call_sid`); every dispatched `ServerMessage` also fans through the new `on_outbound` hook (first-audio latency, recording buffer, monitor tee, `call_ended` → REST hangup detection); a frame-idle timeout (default 30s) now closes a wedged socket the same way the browser heartbeat catches a dead connection |

## 5. Orchestrator (`voicebot/pipeline/orchestrator.py`) — v4 behavior
| Path | v3 | v4 |
|---|---|---|
| batch / streaming turn | `_respond_with_filler`/`_stream_bot_turn` | unchanged — orchestrator itself is untouched; it is driven identically by both `ws/handler.CallSession` and the new `telephony/session.TelephonyCallSession` through the same `send`-callback seam |
| speculative RAG | inline O-5 hold logic on `orchestrator`/`CallSession` | **extracted** to `pipeline/specrag.py::SpeculativeRagCoordinator` (fire on hold / resolve-or-discard via Jaccard-divergence check / cancel on teardown) — `CallSession` keeps its pre-existing inline version; `TelephonyCallSession` is the first consumer of the shared one |
| cost/budget | O-9 `cost.py` | unchanged |

## 6. Call sessions — `ws/handler.CallSession` (browser) and NEW `telephony/session.TelephonyCallSession` (v4)

Both are thin per-call turn-handling-policy owners over the transport-agnostic
orchestrator; `TelephonyCallSession` (453 lines) is explicitly built as the
phone-transport analogue of `CallSession`, closing every gap an audit found
between them:

| Concern | `CallSession` (browser, v3) | `TelephonyCallSession` (telephony, v4 — new) |
|---|---|---|
| Barge-in Gate 1 | client-side VAD worklet reports `barge_in_candidate` + measured speech duration | `on_voice_activity` (buffered ingest, RMS-based — fires once per utterance, approximates "held long enough" via `single_word_interrupt_ms` since there's no partial text to classify) **or** `on_partial` (streaming ingest — the interim STT partial itself is the Gate-1 signal) |
| Barge-in Gate 2 | `BargeInClassifier` direct | `BargeInClassifier` wrapped in `TelephonyBargeInGate` (tracks wall-clock elapsed-since-first-partial as the `speech_duration_ms` proxy) |
| Smart-turn hold + speculative RAG | inline on `CallSession`/`orchestrator` | `SmartTurnAnalyzer.is_complete` + `SpeculativeRagCoordinator` (shared module) |
| Escalation registration + gating | `_sync_escalation` registers `deliver`/`resolve` with `EscalationHub` on the first turn that parks the call in `waiting_agent` | **same pattern, ported** (`_sync_escalation`) — previously nothing called this for telephony at all, so an escalated PSTN call was never reachable by an operator; caller audio during hold is now also dropped (not run through a bot turn) rather than answered over the handoff |
| No-input timeout | precise per-frame timer off the blocking socket receive | 1s-poll `_no_input_watchdog` background task racing `bridge.run()` (no single blocking receive to hang a timeout off); baseline = `max(last_input_at, speech_until)` where `speech_until` is estimated from each chunk's `duration_ms`, mirroring the browser's `playback_done`-driven estimate |
| Call-ending REST hangup | N/A (closing the WS is enough) | `_end_call_leg`: on the orchestrator's `call_ended` send, calls `bridge.hangup()`; if Twilio doesn't already know (`bridge.stopped` false) and a `twilio_call_sid` exists, also calls `warm_transfer.end_twilio_call` (REST `Status=completed`) — Media Streams gives no other way to hang up from the app side |
| Call recording | buffers bot WAV chunks + (v4) customer PCM16→WAV, `concat_wavs` at teardown | same shape: `_buffer_recording_audio` called from `on_outbound` (bot side, already WAV) **and** `on_utterance` (customer side, raw PCM16 wrapped via `pcm16_to_wav`) — captures hold-period speech too, not just answered turns |
| Persistence | `persist_call` at `_finalize` | identical `_finalize` → `persist_call` → `dispatch_followup`/`summarize_and_annotate_crm` (now via shared `postcall.py`, not duplicated) |

`bind(bridge, ingest)` is called after construction, before `run()` — mirrors
`main.py`'s prior ordering (declare bridge → build ingest against the
session's own bound methods → construct bridge).

## 7. AI microservices — provider inventory (v1 → v2 → v3 → v4)

### 7.1 STT (`koel/stt`)
| Class | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| `StubWhisperProvider` | stub | stub | stub | stub |
| `GroqWhisperProvider` | — | real (pinned) | real; batch fallback for streaming | unchanged |
| `SarvamSttProvider` | — | real (batch) | + `stream_transcribe` (verified) | unchanged |
| Deepgram streaming | — | — | real, browser path | **+ telephony path**: `StreamingTelephonyIngest` (O-6 Wave 2) drives the same `DeepgramStreamingSession` over Twilio audio; `main.py::_build_telephony_ingest`/`_resolve_deepgram_streaming` pick it per-call (BYOK connection or env default), falling back to the buffered `TelephonyAudioIngest` on connect failure |
| `WhisperSageMakerProvider` | seam | seam | seam (O-7/6.8) | **real** (`stt/providers.py:734`) — boto3 `sagemaker-runtime.invoke_endpoint`, lazy `import boto3`; no endpoint deployed, so unusable until an AWS SageMaker endpoint exists |
| STT mode | n/a | `chunk` | per-bot `chunk`\|`ws` | unchanged |
| `ingest.UtteranceCallback` | n/a | n/a | `(TranscriptResult) -> None` | **widened** to `(TranscriptResult, bytes) -> None` — the raw PCM16 behind the transcript, needed because telephony's smart-turn gate has no other source of the waveform (Deepgram returns text only) |

### 7.2 NLP (`koel/nlp`)
| Class/feature | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| `StubNlpProvider`/`GroqNlpProvider` | stub / real | stub / +8B/70B | + `generate_stream()` | unchanged |
| route | `/v1/respond` | same | + `/v1/respond/stream` | unchanged |
| streamer | — | — | `nlp/streaming.py` | unchanged |
| BYOK | — | — | custom LLM connection | unchanged |
| Flow generation | — | — | — | **new**: `nlp/flow_gen.py::generate_flow` — synthesizes a conversation flow graph from a bot's RAG-indexed knowledge base; `POST /v1/knowledge/generate-flow` (route-level test coverage added) |

### 7.3 TTS (`koel/tts`)
| Class/feature | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Stub/Orpheus/Magpie/Sarvam/Routing | stub | all real | unchanged | unchanged |
| cache | — | — | `tts_cache.py` | unchanged |
| `OrpheusSageMakerProvider` | seam | seam | seam | **real** (`tts/providers.py:750`) — same boto3 pattern as the Whisper SageMaker provider; no endpoint deployed |
| `mixer.py` | `concat_wavs` (bot-only recording) | same | same | **+ public `pcm16_to_wav` helper** (wraps raw PCM16 in a WAV container) — used by both transports to fold customer audio into the recording buffer alongside the bot's |

### 7.4 RAG embedder (`nlp/rag.py`)
| Class | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| `HashEmbedder` | stub | stub | stub (fallback) | stub (fallback) |
| NVIDIA | — | real | real | real (live default) |
| `FastEmbedEmbedder` | — | — | real, local `bge-m3` | unchanged |
| `BedrockTitanEmbedder` | — | seam (footgun) | seam (fail-loud-at-boot fixed) | **real** (`nlp/rag.py:160`) — Bedrock Titan Embeddings V2, `ap-south-1`, `boto3.client("bedrock-runtime")` built once, `invoke_model` off the event loop via `asyncio.to_thread`; needs a Bedrock model-access grant to actually serve traffic |

### 7.5 CRM (`koel/crm`)
Unchanged in v4. Post-call CRM annotation (`summarize_and_annotate_crm`, 5.3)
moved into shared `postcall.py` but its behavior is identical.

## 8. Configuration reference (`KOEL_` env) — v4 additions
`telephony_no_input_reprompt_ms`/`telephony_no_input_close_ms` (telephony
no-input watchdog thresholds), `telephony_frame_idle_timeout_s` (Media Streams
idle-socket close), `google_client_id`/`google_jwks_url`/`google_allowlist`
(Google Sign-In), Twilio dialer settings (`twilio_default_bot_id`, calling-hours
enforcement knobs already present, now actually exercised by
`dialer_worker.py`). All default-off/inert so v3 behavior is preserved;
Google login itself 403s (`google_login_disabled`) unless `google_client_id`
is set.

## 9. REST API — v4 additions
+ `POST /v1/auth/google` (Google Sign-In, allowlist, rate-limited fail-closed);
+ `POST /v1/users/invite`, `POST /v1/users/accept-invite`, `PATCH
/v1/users/{id}`, `POST /v1/users/{id}/{deactivate,reactivate}`, `POST
/v1/users/{id}/reset-password`, `POST /v1/users/{id}/resend-invite`; +
`GET/POST /v1/orgs`, `GET /v1/orgs/{id}`, `POST /v1/orgs/{id}/deactivate`; +
`POST /v1/knowledge/generate-flow`; + `POST
/v1/operators/escalations/{id}/warm-transfer` (PSTN Conference handoff, rate
limited, claim-owner-only, 409 if the call has no PSTN leg); telephony's
`/twilio/stream` WS route is now wired to `TelephonyCallSession` end-to-end
(previously a set of closures directly in `main.py`).

## 10. Key algorithms — v4 additions
- **Speculative-RAG divergence check** (`specrag.transcripts_diverged`) — pure,
  deterministic Jaccard-overlap ≥ 0.6 (or prefix/continuation match) decides
  reuse-vs-discard; unchanged logic, now a standalone reusable unit instead of
  inline on `orchestrator`.
- **RMS-gated telephony endpointing** (`telephony/ingest.py`) — buffer →
  trailing-silence detection → flush-to-transcribe state machine; documented
  as a structure that is production-tested but whose RMS/silence thresholds
  are a starting point pending real PSTN comfort-noise tuning.
- **No-input watchdog via playback-finish estimate** — telephony has no
  `playback_done` signal from the carrier, so `_speech_until` (accumulated
  from each outbound chunk's declared `duration_ms`, same math as the
  browser's estimate) stands in for it in the no-input baseline calculation.
- **REST-driven PSTN hangup** (`warm_transfer.end_twilio_call`) — the missing
  mechanism for an app-initiated call end on a transport with no equivalent
  to "just close the socket."
- **Bidirectional recording mux** — both transports now buffer bot WAV chunks
  (from the outbound send path) and customer WAV chunks (from the
  finalized-utterance path, PCM16→WAV via the new public `pcm16_to_wav`),
  `concat_wavs`'d into one clip at teardown.
- **Campaign dial-queue claim** — `FOR UPDATE SKIP LOCKED` on Postgres (no
  double-dialing across workers), degrading to a windowed select+update on
  SQLite for tests.

## 11. Testing strategy (`backend/tests`, 913 tests) — v1 → v2 → v3 → v4
37 → 264 → 680 → **913 passing, 1 skipped, 0 failing** (91.87% statement
coverage against an 85% CI gate; `ruff check .` and `mypy koel` both clean
across 118 source files). New v4 suites: telephony parity (barge-in Gate 1 for
both ingest paths, no-input watchdog, escalation registration/gating, REST
hangup, idle-frame watchdog, bidirectional recording on both transports),
`specrag.py`/`postcall.py` unit tests (extracted-module coverage), route-level
coverage for `POST /v1/knowledge/generate-flow`, users API (invite/accept/
manage) + Google auth, outbound dialer (claim/originate/record-result),
Redis-backed escalation/monitor (pub/sub fan-out), 4 bug-hunt-batch regression
tests (cross-tenant IDOR, WS finalize/data-loss, telephony frame-parse
hardening), AdminView/AcceptInviteView/auto-flow-builder frontend e2e.
Previously weak spots specifically targeted: `telephony/carrier.py` (was 0%),
`knowledge.py`, `users.py`, `auth.py`. Remaining lower-coverage files (all
still reasonable): `combined.py` (53%, boot-sequencing), `dialer_worker.py`
(62%), `telephony/carrier.py` (70%), `exports.py` (72%), `clients.py` (76%).

## 12. Extension seams — v4
Telephony is **no longer a seam** — `telephony/session.py` is a complete,
tested, feature-parity transport, not a future integration point; treat
anything describing it as a "seam" (including this doc's own v3 §12 and
`docs/TELEPHONY.md`/`TELEPHONY_V3.md`) as superseded. Remaining open seams:
**SageMaker endpoints** (O-7/6.8 — code paths (`WhisperSageMakerProvider`,
`OrpheusSageMakerProvider`) and Terraform are real, but no endpoint is
deployed against a real model artifact); **AWS Bedrock model access**
(`BedrockTitanEmbedder`'s code path is real, needs a model-access grant in
`ap-south-1`); **data residency** (the compliance ruling, not code — see
`docs/RESIDENCY.md`); **AWS Secrets Manager/SSM** (Terraform documents the
wiring via the ECS task definition's `secrets` block but does not implement
it — env vars only, today); `telephony/exotel.py` (a second carrier adapter,
present but far less exercised than the Twilio path).
