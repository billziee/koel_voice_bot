# KOEL Voice Bot — Handoff Document (v4_final)

**As of:** commit `5b369a0`, 2026-07-22 · **Status:** actively deployed, in active handoff
**Read this first.** This is the single, authoritative, self-contained handoff document. It supersedes `HLD_V2.md`, `HLD_V3.md`, `LLD_V2.md`, `LLD_V3.md`, and `TELEPHONY.md`/`TELEPHONY_V3.md` on anything to do with the telephony pipeline — those docs predate PR #65 and describe Twilio calling as an unbuilt "seam." It is not built as a seam anymore; it's a complete, tested, feature-parity transport. Section 8 maps which older docs are still trustworthy for what.

---

## 1. Executive summary

KOEL is a bilingual (English/Hindi/Hinglish) voice bot for Indian call-center use cases (credit-card collections, insurance renewal), reachable over **both a browser WebSocket client and real PSTN phone calls (Twilio Media Streams)**. As of this handoff, both transports run the identical call-handling pipeline — barge-in, semantic turn-holding, speculative RAG, human escalation with warm transfer, live supervisor monitoring, and full bidirectional call recording — with no feature gap between "the demo you can click through in a browser" and "an actual phone call a customer places or receives."

The backend is a FastAPI monolith (five logical services, one deployed process) backed by Postgres + Redis, live on Render today. The frontend is a React/Vite SPA. AI inference (STT/NLP/TTS) is provider-pluggable; the live deployment runs Groq for the primary pipeline and Sarvam for India-hosted Hindi/Hinglish legs.

**The single biggest open item is not code — it's a legal/compliance ruling** on data residency (Section 5), plus the mechanical follow-through of rotating dev-exposed API keys (Section 4) before this is used for real customer calls.

---

## 2. System architecture

### 2.1 Service topology & deployment shape

Five logical FastAPI services live under `backend/koel/`:

| Service | Package | Port (dev) |
|---|---|---|
| Orchestrator / call API | `voicebot` | 8100 |
| Speech-to-text | `stt` | 8101 |
| NLP (intent + response + RAG) | `nlp` | 8102 |
| Text-to-speech | `tts` | 8103 |
| CRM | `crm` | 8104 |

**In production these run as one process.** `backend/koel/combined.py` mounts all five sub-apps into a single `FastAPI` instance, runs each sub-app's lifespan manually via `AsyncExitStack`, and self-migrates/seeds Postgres on boot (Render's free tier has no pre-deploy hook to run migrations separately). Dev/CI uses five separate Docker Compose containers instead (`backend/docker-compose.yml`) — same code, different topology.

### 2.2 The two call transports — now at parity

- **Browser:** `koel/voicebot/ws/handler.py` — `CallSession` (the original, reference implementation).
- **Telephony (Twilio):** `koel/voicebot/telephony/session.py` — `TelephonyCallSession`, built this cycle explicitly as "the phone-transport analogue of `ws/handler.CallSession`."

Both transports are thin adapters over the same transport-agnostic pipeline:

| Component | File | Shared behavior |
|---|---|---|
| Orchestrator | `voicebot/pipeline/orchestrator.py` | Drives every turn via `handle_final_transcript`/`end_call`, transport-blind (works through an injected `send` callback) |
| Barge-in | `voicebot/pipeline/bargein.py` (`BargeInClassifier`) | Dual-gated interruption (backchannel dictionary + word/duration thresholds). Telephony wraps it in `telephony/bargein_gate.py` to handle both the RMS-voice-activity case (buffered STT ingest, no true partials) and the STT-partial case (streaming ingest) |
| Smart-turn hold | `voicebot/pipeline/endpointing.py` (`SmartTurnAnalyzer`) | Holds a mid-thought transcript and merges the follow-up instead of answering half a sentence, on both transports |
| Speculative RAG | `voicebot/pipeline/specrag.py` (`SpeculativeRagCoordinator`) | Fires KB retrieval during the smart-turn hold window so it's ready by the time the turn actually runs |
| Escalation / warm transfer | `voicebot/escalation.py` (`EscalationHub`) + `voicebot/pipeline/warm_transfer.py` | Registers a live deliver/resolve callback with the operator console; telephony additionally supports a REST-driven Twilio Conference warm transfer and a REST hangup (`end_twilio_call`) for orchestrator-initiated call endings (opt-out, no-input close, budget cap) |
| Live supervisor monitor | `voicebot/monitor.py` (`LiveMonitor`) | Real-time call visibility for a supervisor dashboard, Redis pub/sub-backed for multi-worker |
| Persistence | `voicebot/persistence.py` | `persist_call` writes `CallLog` + `TranscriptTurn` rows; both transports call it at teardown |
| Call recording (both sides) | — | Both transports now buffer **bot AND customer** audio (not just the bot's half — this was a real, recently-closed gap) and mux to one WAV via `tts/mixer.concat_wavs`, uploaded to S3 if configured |
| No-input timeout | — | Browser uses a precise per-frame timer; telephony polls a 1s watchdog against the same reprompt/close thresholds (`pipeline/silence.on_no_input`) — functionally equivalent, mechanically different since telephony has no single blocking receive call to hang a timeout off of |

Telephony-only transport mechanics (no browser equivalent, none needed): `telephony/bridge.py` (`TwilioMediaBridge` — Media Streams protocol, µ-law↔PCM16 transcoding, DTMF), `telephony/twilio.py` (signature verification), `telephony/ingest.py` (server-side RMS endpointing for the non-streaming STT path), `telephony/exotel.py` (a second carrier adapter, less exercised than Twilio).

### 2.3 Data stores

- **Postgres** (`koel/db/models.py`, ~21 tables): `Org`, `User`, `ModelConnection` (BYOK), `CrmCustomer`, `BotConfig` (+audit trail), `CallLog`, `TranscriptTurn`, `EscalationRequest`, `ConsentRecord`, `Campaign`/`DialAttempt` (outbound dialer), `KnowledgeChunk` (pgvector RAG), `ExportJob`.
- **Redis**: session state (`voicebot/session/store.py`, TTL-based, powers the browser's 15s reconnect grace window), plus optional cross-process escalation delivery and live-monitor fan-out via pub/sub (dormant/degrades gracefully without Redis; required for anything beyond a single worker process).

### 2.4 Frontend (`src/`)

React/Vite SPA: login, dashboard, live active-call view (transcript + waveform), bot config, a visual bot/flow builder, knowledge-base management, call history, BYOK model-connection management, and an admin panel (org/user management, invites). Real microphone capture with a Silero VAD worklet (`src/lib/vad.ts`) — this is what supplies the raw PCM16 the backend uses for smart-turn analysis and (now) call recording.

### 2.5 AI provider integrations

Every provider is pluggable via env var; a **fresh checkout defaults every capability to a `"stub"` provider** (no real AI calls, useful for tests/dev). The **live deployment** sets real providers:

| Capability | Live default | Also available | Status |
|---|---|---|---|
| NLP | Groq (`GroqNlpProvider`) | Sarvam | Real |
| STT | Groq Whisper | Sarvam, Deepgram (streaming) | Real |
| TTS (English) | Groq Orpheus | — | Real |
| TTS (Hindi/Hinglish) | NVIDIA Magpie | Sarvam, Deepgram, generic OpenAI-compatible | Real |
| Embeddings (RAG) | NVIDIA NIM | AWS Bedrock Titan v2 | Real (Bedrock now implemented, not just a stub — needs a Bedrock model-access grant to actually use) |
| STT/TTS on SageMaker | — | Whisper/Orpheus SageMaker providers | **Code is real (boto3), but no endpoint is deployed** — Terraform for this exists but was never applied |

---

## 3. Feature status

### 3.1 Fully built and active by default (no config needed beyond a boot)

Core orchestrator, both call transports end-to-end, session lifecycle (reconnect grace, heartbeat, idle-socket detection), dual-gated barge-in, filler-audio latency masking, RAG retrieval, EMI calculator, live supervisor monitor, and full bidirectional call recording (once an S3 bucket is set — see 3.2).

### 3.2 Built but dormant until configured

| Feature | Activates via |
|---|---|
| Twilio/Exotel inbound + outbound calling, warm transfer, DTMF | `KOEL_TWILIO_ACCOUNT_SID` / `KOEL_TWILIO_AUTH_TOKEN` / `KOEL_TWILIO_FROM_NUMBER` (or Exotel equivalents) — the WS route fails closed with no token configured |
| BYOK per-bot model connections (custom LLM/TTS/STT) | `KOEL_SECRET_KEY` (Fernet key) — unset means BYOK is unavailable, not a boot failure |
| Sarvam India-hosted STT/TTS | `KOEL_SARVAM_API_KEY` |
| Deepgram streaming STT (true low-latency partials) | `KOEL_DEEPGRAM_API_KEY` + `stream_stt_provider=deepgram` (default streaming path is Sarvam's WS endpoint) |
| Call-recording upload to S3 | `s3_recording_bucket` — empty means recording is buffered in memory but never uploaded (logged no-op) |
| Cross-process escalation/monitor fan-out (multi-worker) | a Redis client is always wired; without it, escalation/monitor state is single-process only |

### 3.3 Genuinely not built / blocked on external action

These need something outside this codebase, not more engineering:

- **SageMaker endpoints** for Whisper/Orpheus in-region hosting — needs a real AWS account + GPU endpoint deployment + a real model artifact. Terraform is written (`infra/terraform/`) but never applied; deliberately not faked with placeholder values.
- **AWS Bedrock model access** for the in-region Titan embedder — needs a Bedrock model-access grant in `ap-south-1`.
- **RBI/DPDP/IRDAI data-residency sign-off** — a legal ruling, not code (Section 5).
- **A real S3 recording bucket** — currently unset; recording buffering works, upload is a no-op without it.
- **Live Twilio/Exotel production account + DID** — the code path is real and tested; there's no live carrier account behind it yet.
- **SMS/WhatsApp/payment-provider accounts** — payment-link SMS needs an SMS provider + India DLT template registration; the WhatsApp follow-up needs an approved WhatsApp Business Account. Pure procurement.
- **A real client CRM to integrate against** — the adapter interface exists; there's no live CRM on the other end.

---

## 4. Security & credentials — READ THIS FIRST

### 4.1 Credentials that MUST be rotated before any real production use

During this project's development, the following were pasted in plaintext during chat/debugging sessions at some point and must be treated as compromised:

1. **Groq API key**
2. **NVIDIA NVCF key**
3. **Sarvam AI key**
4. **Deepgram key**
5. **Render deployment API token** (`rnd_...`, used to trigger deploys — this itself is a credential, not just an app secret)

`docs/SECURITY.md`'s pre-production checklist tracks this explicitly — check it before go-live; don't assume this list is stale by the time you read it.

### 4.2 Secret management: today vs. required

- **Today:** environment variables only. `backend/.env` (local, correctly gitignored, never committed) and Render's dashboard-managed env vars are the only mechanisms in use.
- **AWS Secrets Manager / SSM is NOT wired.** The Terraform has comments describing how to wire it (via the ECS task definition's `secrets` block), but it's guidance for whoever builds the AWS deployment, not a working integration. If/when this moves to AWS, this is a real gap to close before go-live, not an oversight to assume is already handled.
- `KOEL_SECRET_KEY` (BYOK Fernet key) now warns at boot in production if unset — check deploy logs for it.

### 4.3 Accepted risks (signed off, not oversights — don't re-litigate without new information)

- **Cross-tenant username/email enumeration**: `POST /v1/users/invite` leaks one bit (does this identifier exist anywhere on the platform) due to globally-unique columns inherited from the original single-tenant schema. Requires `role=admin` auth, rate-limited, no pre-auth path. Explicitly accepted as disproportionate to fix given the access level required.
- **Data residency** (see Section 5) — the big one, tracked separately because it's compliance-blocking, not just a security nit.

---

## 5. Compliance status (DPDP / RBI / IRDAI)

| Domain | Status |
|---|---|
| DPDP Act 2023 (consent, minimisation, PII masking) | 🟢 Met |
| TRAI (DND scrubbing, opt-out) | 🟢 Met |
| RBI collections (self-ID, calling hours) | 🟡 Self-ID met; calling-hours enforcement is built and correct but **opt-in, off by default** so demos aren't blocked — turn it on before real collections calls |
| IRDAI insurance (disclosure, consent capture) | 🟡 Disclosure + timestamped consent met; evidentiary consent recording tied to a specific audio offset is not yet a distinct artifact |
| **Data residency (AWS ap-south-1 required)** | 🔴 **Not met — the single most significant open compliance item.** NLP + English STT/TTS run on US-hosted Groq/NVIDIA. Sarvam (India-hosted) now covers Hindi/Hinglish STT/TTS as a partial mitigation, but its DPA/region should be confirmed in writing, not assumed compliant just because it's geographically convenient. Closing this fully means either expanding Sarvam's role or self-hosting in-region — both are engineering work waiting on the legal ruling below, not blocked technically. |

**This needs an actual legal/compliance ruling before any real customer PII flows through the current US-hosted inference path at scale.** Everything else in this table is either done or a config flip; this one item is the load-bearing blocker for a real go-live in a regulated Indian financial-services context.

---

## 6. Deployment & infrastructure

### 6.1 Current live deployment

Live on Render right now: frontend and backend are both deployed and wired together. Backend runs `koel.combined:app` (the single-process topology) to fit inside a free/starter tier's memory budget, and self-migrates its schema on boot. `scripts/deploy_render.sh` is a scripted alternative to the dashboard/git-push deploy path — reads `RENDER_API_KEY` from the environment (never hardcode it), triggers a deploy via Render's REST API, and polls for completion.

**Known drift to fix or consciously accept:** the backend and frontend Render services were, at last check, tracking different git branches (backend on `main`, frontend on a feature branch) — confirm this is still true and reconcile it; it's a "someone forgot to flip a setting" gap, not an intentional design.

### 6.2 Docker

`backend/Dockerfile` is a working multi-stage build: builds into an isolated venv, runs as a non-root user (`useradd --uid 10001 koel`), has a container `HEALTHCHECK` against `/health/live`. Good to build from as-is.

### 6.3 Terraform (`infra/terraform/`) — written, never applied

No state file exists anywhere in the repo (correctly gitignored) and the code itself says outright it's never been run against a real AWS account. It provisions, for an `ap-south-1` AWS deployment: an S3 recordings bucket (KMS-encrypted, 30-day lifecycle), RDS Postgres (Multi-AZ, encrypted, deletion-protected), ElastiCache Redis (Multi-AZ, encrypted, AUTH), least-privilege IAM (S3 + a Bedrock-Titan-only `InvokeModel` grant), and an ECS Fargate service running the same combined-app image. **Deliberately does not provision SageMaker endpoints** — the code's own comment explains that inventing placeholder values for a real model artifact would be Terraform that "looks complete but points at nothing," which is worse than an honest gap.

Whoever picks up the AWS migration: this is a solid starting point, not a stub — review it, fill in the real values (VPC, subnets, ALB target group are deliberately left as inputs to an existing network, not created here), and `terraform apply` when the residency decision (Section 5) is actually made.

### 6.4 Minimum env vars to boot

Every setting has a Pydantic default, so nothing hard-fails class construction — but for a real deployment you need at minimum: `KOEL_ENVIRONMENT=production`, provider keys for whichever of Groq/NVIDIA/Sarvam you're actually using, `KOEL_JWT_SECRET` (the app should refuse the dev default in production — verify this still holds), `KOEL_CORS_ALLOW_ORIGINS`, `KOEL_DATABASE_URL`, `KOEL_REDIS_URL`. See `backend/.env.example` for the full list of ~34 recognized vars with placeholder values.

### 6.5 Known operational gotchas

- **Render env-var updates during an in-flight deploy don't reach the running container** — if you change an env var while a deploy is already in progress, force a fresh deploy afterward rather than assuming the change took effect.
- **GitHub Actions CI is currently blocked** (observed directly this session: PR checks fail in ~3 seconds with zero steps executed — consistent with an account-level billing/spending-limit block, not a code or test failure). This is unrelated to code correctness — verify locally (`ruff check .`, `mypy koel`, `pytest`) rather than trusting CI's green/red status until this is resolved. Whoever owns the GitHub org's billing needs to fix this; it isn't something a code change can work around.

---

## 7. Testing & code quality (freshly measured, not from stale docs)

- **913 tests passing, 1 skipped, 0 failing.** Full suite runs in ~40s.
- **91.87% statement coverage** against an 85% CI gate (`pyproject.toml`'s `[tool.coverage.report] fail_under = 85`) — comfortably above threshold.
- Lowest-coverage files (still all reasonable, nothing alarming): `combined.py` (53% — mostly boot-sequencing code that's hard to unit-test), `pipeline/dialer_worker.py` (62%), `telephony/carrier.py` (70%), `voicebot/exports.py` (72%), `voicebot/clients.py` (76%).
- `ruff check .` — clean, zero violations.
- `mypy koel` — clean, zero errors across 118 source files.

If any of these regress, that's a real signal — this whole codebase has been kept green throughout its development, not just at checkpoints.

---

## 8. Documentation map — what's current vs. stale

| Doc | Status |
|---|---|
| **This file** | Current, authoritative for handoff |
| `docs/SECURITY.md` | Current — the credential-rotation checklist and security posture here are accurate and actively maintained |
| `docs/RESIDENCY.md` | Current on the residency gap analysis (Section 5 above summarizes it) |
| `docs/PRD_V2.md` | Mostly current for product requirements and the compliance table; cross-check anything telephony-specific against this doc and the source, since even PRD_V2 predates the final telephony parity work |
| `docs/FUTURE_PROCEEDINGS.md` | A roadmap **snapshot** — several items it lists as future work (outbound dialer, PSTN warm transfer, Twilio bridge, BYOK connections) are now built and tested. Treat it as historical context, not a current backlog, without cross-checking the source first |
| `docs/HLD_V2.md`, `docs/HLD_V3.md`, `docs/LLD_V2.md`, `docs/LLD_V3.md` | **Stale on telephony** — describe Twilio/Exotel as an unbuilt seam. Everything else in them (browser transport, data model, service boundaries) is still broadly accurate |
| `docs/TELEPHONY.md`, `docs/TELEPHONY_V3.md` | **Superseded** — telephony is fully built now; read `koel/voicebot/telephony/session.py` and this handoff's Section 2.2 instead |
| `docs/PRODUCTION_MASTERPLAN.md` | Current for the pre-go-live checklist (rotate keys, residency ruling, `terraform apply`, staging, TLS everywhere, Redis rate-limiter, compliance sign-off) |
| `docs/DEPLOY.md` | Current for the Render deployment mechanics |
| `docs/SLO.md`, `docs/LOADTEST.md` | Not verified as part of this handoff pass — review before relying on them |

---

## 9. Immediate next steps for the incoming team

1. **Rotate the five credentials in Section 4.1** before doing anything else with real data.
2. **Get the data-residency legal ruling** (Section 5) — this gates whether the current US-inference architecture is acceptable to keep, or whether Sarvam's role needs expanding / self-hosting needs to start.
3. **Resolve the GitHub Actions billing block** so CI actually runs again — don't fly blind on green-checkmark trust until then.
4. **Reconcile the backend/frontend branch drift** on Render (Section 6.1).
5. Everything else in `docs/PRODUCTION_MASTERPLAN.md`'s checklist (staging environment, TLS everywhere, Redis-backed rate limiting confirmed set, `terraform apply` if/when going to AWS) is real, scoped, and unblocked by anything above — pick up whenever capacity allows.
