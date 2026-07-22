# KOEL Voice Bot — High-Level Design v4

> Supersedes [HLD_V3.md](./HLD_V3.md) (which superseded [HLD_V2.md](./HLD_V2.md),
> which superseded [HLD.md](./HLD.md)). v4 is the current state after PR #65 —
> telephony reaching **full call-handling parity** with the browser transport —
> plus the outbound dialer, real multi-tenant org/user administration, Google
> Sign-In, four more security bug-hunt batches, and real (not stubbed) AWS
> SageMaker/Bedrock provider code. Every table below now carries a **v1 / v2 /
> v3 / v4** column so the evolution is legible end-to-end.

## What changed since v3 — at a glance

v3 attacked latency and product depth but left three things as seams rather
than builds: telephony (an ingest path with no call-handling policy behind
it), outbound calling and tenant admin (schema-only), and AWS in-region
providers (stubs). v4's largest single change, by far, is **PR #65**: telephony
now runs the identical call-handling pipeline as a browser call — barge-in,
smart-turn hold, speculative RAG, gated human escalation, a no-input watchdog,
and (a real, previously-missing gap on **both** transports) bidirectional call
recording — see `koel/voicebot/telephony/session.py`'s `TelephonyCallSession`.
Around that: the outbound dialer now originates real PSTN calls end-to-end,
multi-tenant admin gained a genuine invite/accept/manage flow plus Google
Sign-In, four more bug-hunt hardening batches ran (one a critical cross-tenant
IDOR), and the AWS SageMaker/Bedrock provider code is now real boto3 — though
no endpoint is deployed and **data residency itself is unchanged, still the
single biggest open compliance item.** Test count went 680 → **913**.

| Area | v1 (HLD.md) | v2 (HLD_V2.md) | v3 (HLD_V3.md) | v4 (now) |
|---|---|---|---|---|
| **STT** | Stub (type-to-talk) | Groq Whisper (pinned) + Sarvam `saaras` (batch) | + Deepgram streaming + per-bot `chunk`\|`ws` mode + BYOK custom STT | unchanged provider set; both telephony ingest paths (buffered RMS + Deepgram-streaming) now actually drive a live PSTN call end-to-end via `TelephonyCallSession` — previously wired to nothing that ran a call |
| **NLP** | Real Groq (only real piece) | Language-routed 8B/70B, JSON-mode | + O-2 first-token streaming; BYOK custom LLM | unchanged core; **+ `flow_gen.generate_flow`** drafts a conversation-flow state machine from a bot's KB chunks (auto flow-builder, `POST /v1/knowledge/generate-flow`) |
| **TTS** | Stub (sized PCM) | 3 engines, Sarvam WS default | + O-3 response cache; BYOK custom TTS | unchanged (3 engines + cache + BYOK); bot audio is now also buffered on both transports for the bidirectional call recording |
| **RAG embeddings** | `HashEmbedder` stub | NVIDIA `nv-embedqa-e5-v5` | + O-4 local FastEmbed; + O-5 speculative retrieval | **+ AWS Bedrock Titan v2 embedder now a real boto3 implementation** (was `NotImplementedError`) — needs a Bedrock model-access grant in `ap-south-1` to actually use; **O-5 extracted into a shared `SpeculativeRagCoordinator`** (`pipeline/specrag.py`) so telephony reuses the identical fire/resolve/cancel logic instead of a bespoke copy |
| **Turn latency** | "sub-300ms is the product" (thesis) | Renegotiated + measured | Token-bound path: O-1…O-6 | unchanged token-bound path; first-audio latency now also recorded for telephony calls (`record_first_audio_latency` wired into `TelephonyCallSession`) |
| **Auth / tenancy** | JWT REST+WS | +`/refresh` (FE gap) | Q-2 persistence + silent refresh; real Users table + enforced org scoping | **+ Sign in with Google** (allowlist model, JWKS-verified ID token, rate-limited fail-closed) alongside password auth; **+ real org/user admin flow** — `/v1/orgs` (tenant registry CRUD) + `/v1/users` (invite/accept/manage) with platform-admin-vs-tenant-scoped-admin gating, replacing v3's schema-only seam |
| **Rate limiting** | In-memory, single-worker | Same | + O-8 Redis backend | unchanged |
| **Cost controls** | None | None | + O-9 per-call economics + budget caps | unchanged |
| **DB / retrieval** | Default pool | Default pool | + O-10 pool observability + pgvector `ef_search` + LIFO | **+ outbound dialer's `FOR UPDATE SKIP LOCKED` claim queue** (Postgres; SQLite degrades to a windowed select+update) — a new concurrency-safe access pattern alongside the existing pool/pgvector knobs |
| **Frontend** | Type-to-talk SPA | Real mic + Silero VAD + barge-in | Q-1 decomposed + code-split; connections manager + flow editor | **+ AdminView** (org/user management, accept-invite flow) **+ auto flow-builder** UI drafting a flow into the existing editor |
| **Human-in-the-loop** | — | — | 3.1 live agent handoff (browser text-relay) + 3.2 inbound + 3.3 supervisor monitor | **+ real PSTN warm transfer** (Twilio Conference API: redirect the live customer leg + a whisper-then-join operator leg — not TaskRouter, which needs a provisioned Workspace this project has no account for) **+ telephony escalation gating fixed** — `on_utterance` now drops caller audio while `waiting_agent`/`ended`; previously the bot kept running full turns and talking straight over a customer already on hold |
| **Telephony** | Browser WS only | Browser WS only | Streaming-session ingest seam (`telephony/ingest.py`); bridges "the next build" | **Full transport parity, seam retired.** `TelephonyCallSession` (`telephony/session.py`, ~450 lines) is the phone-transport twin of `ws/handler.CallSession`: dual-gated barge-in (RMS Gate 1 for the buffered path, shared `BargeInClassifier` Gate 2 for both), smart-turn hold + speculative RAG, a no-input watchdog, gated human-handoff, a REST-driven hangup (`end_twilio_call`) for orchestrator-initiated endings, an idle-frame watchdog on the carrier socket (`TwilioMediaBridge`, 30s), Redis `CallSid` persistence, DTMF (`telephony/dtmf.py`), and — previously missing outright — **telephony calls are now actually persisted to Postgres**, and **both transports now record both sides of the conversation** (bot AND customer audio; previously only the bot's half, on either transport) |
| **Outbound calling** | out of scope | out of scope | out of scope | **Built**: campaign dial-queue (`campaigns.py`, claim/retry policy, `FOR UPDATE SKIP LOCKED`) + `dialer_worker.py` origination loop (Twilio REST Calls API via `pipeline/outbound_dialer.py`) — DND-scrubbed and RBI-calling-hours-gated before every attempt; dormant until Twilio is configured |
| **Multi-tenant admin** | — | — | Schema seam (`org_id` scoping, no registry UI) | **Built**: `/v1/orgs` (platform-admin-only tenant registry CRUD) + `/v1/users` (invite/accept/manage, platform-admin vs tenant-scoped-admin gating) + AdminView UI |
| **AWS in-region infra** | — | — | Design only (O-7, legal-gated) | SageMaker Whisper/Orpheus + Bedrock Titan provider code now real (boto3); Terraform for the full `ap-south-1` stack (S3/RDS/ElastiCache/IAM/ECS Fargate) written and validated — **`terraform apply` never run**; SageMaker itself deliberately not provisioned (no real trained model artifact to point at) |
| **Data residency (compliance)** | Design (PRD §11) | Same | Open — hosted US Groq/NVIDIA inference; Sarvam partial mitigation for HI/Hinglish | **Unchanged, still open — the single biggest compliance gap.** The Bedrock embedder being real code is an implementation-readiness change only; it does not move the compliance needle without the legal residency ruling |
| **Security hardening** | — | — | Pre-v3 bug-hunt (tenant-isolation IDOR, shutdown/data-loss, never-drop-turn) | **+ 4 more bug-hunt batches**: critical cross-tenant IDOR + high-sev correctness/security, WS finalize/data-loss cluster, medium/low correctness+authz, telephony frame-parse hardening — plus a Twilio signature-verification fix and a stateful-codec resample bug fix. **New accepted risk**: cross-tenant username/email enumeration via `POST /v1/users/invite` (narrow, admin-gated, rate-limited — see `SECURITY.md`) |
| **TypeScript** | loose | loose | Q-4 full `strict` | unchanged |
| **CI** | chromium-only, advisory | chromium-only | Q-5 cross-browser gate, blocks merge | **GitHub Actions currently blocked** (PR checks fail in ~3s with zero steps run — consistent with an account-level billing/spending-limit block, not a code issue); verify locally (`ruff check .`, `mypy koel`, `pytest`) until resolved |
| **Tests** | 37 | 264 | 680 | **913** (1 skipped, 0 failing; 91.87% coverage vs. an 85% CI gate) |

See [HANDOFF_V4_FINAL.md](./HANDOFF_V4_FINAL.md) for a synthesized,
cross-checked current-state reference (read that first for anything
telephony-related — it supersedes this doc and `TELEPHONY_V3.md` on that
topic), and [TELEPHONY_V4.md](./TELEPHONY_V4.md) for the full telephony
build log. [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md) is now a stale
**snapshot** — several items it lists as future work (outbound dialer, PSTN
warm transfer, Twilio bridge, BYOK) are built; treat it as historical
context, not a current backlog, without cross-checking source first.

---

## 1. Purpose & scope

Unchanged from v1/v2/v3: a multilingual (EN / HI / Hinglish) outbound
voice-bot for Indian financial services — 5 FastAPI microservices (STT, NLP,
TTS, CRM, VoiceBot orchestrator) behind a React SPA, run combined as
`koel.combined:app` on the free tier. v4 turns three of the platform layer's
remaining seams — telephony, outbound calling, tenant admin — into real,
tested builds, and adds Google Sign-In alongside the existing multi-tenant/
BYOK layer v3 introduced.

## 2. Goals & non-functional requirements

| Dimension | v1 target | v2 measured/renegotiated | v3 direction | v4 state |
|---|---|---|---|---|
| First-audio p50 | <300ms (turn) | ~1.0s (perceived <300ms via filler) | Token-bound; O-2+O-6 shrink the true floor | unchanged direction; now also measured on telephony calls (`record_first_audio_latency` wired into `TelephonyCallSession`) |
| Concurrency | design-only | 0% err @ 10 concurrent (measured) | O-8 (Redis limiter) + O-10 (pool) | **+ outbound dialer claim concurrency** via Postgres `FOR UPDATE SKIP LOCKED` (SQLite degrades to a windowed select+update for single-worker dev/test) |
| Cost visibility | none | none | O-9 per-call estimate + session ceiling | unchanged |
| Multi-tenant | seam | seam | Enforced org scoping at login + per-row (6.4) | **Real admin flow**: invite/accept/manage API + AdminView UI (platform vs tenant-scoped gating) + Google Sign-In (allowlist) |
| Never-drop-a-turn | design | tested (264) | Preserved through streaming/speculative paths (680 tests) | Extended to telephony: no-input watchdog, idle-frame watchdog, and abnormal-disconnect persistence now give the phone transport the same guarantee (913 tests) |
| **Feature parity across transports** *(new)* | — | — | — | **Achieved**: browser and telephony run the identical turn-handling pipeline (barge-in, hold, speculative RAG, escalation, recording) — the single biggest v4 goal, met via PR #65 |

## 3. System context

The five services and the SPA are unchanged in shape. New **external
dependencies/interactions** in v4: **Google Identity Services** (JWKS
verification for Sign-In), **Twilio's REST Calls + Conference APIs** now
actually *invoked* (not just ingested) for outbound origination, warm
transfer, and orchestrator-initiated hangup, and **boto3/AWS SDK** clients
(`sagemaker-runtime`, `bedrock-runtime`) for the optional in-region provider
code — all still dormant/off by default, none live-deployed.

## 4. Component responsibilities (v4 deltas)

| Component | v2 | v3 additions | v4 additions |
|---|---|---|---|
| VoiceBot orchestrator | turn loop, resilience, filler/barge-in | O-2 streaming turn, O-5 speculative-RAG hold, O-9 cost/budget guard, EscalationHub + LiveMonitor wiring | Shared `postcall.py` (CRM summary + follow-up dispatch) and `SpeculativeRagCoordinator` (`pipeline/specrag.py`) extracted so both transports reuse identical logic instead of the browser keeping a bespoke inline copy |
| WS handler (browser) | buffered per-utterance STT | + streaming ingest, serialized streaming finals | Now also buffers customer audio (not just bot's) for bidirectional call recording — a gap that existed on **this** transport too, not just telephony |
| Telephony *(new component)* | — | Ingest seam only (`telephony/ingest.py`) | **`TelephonyCallSession`** (`telephony/session.py`) + **`TwilioMediaBridge`** (`telephony/bridge.py`) — full call-handling policy + transport mechanics: barge-in, hold+speculative RAG, no-input watchdog, gated escalation, idle-frame watchdog, DTMF, Redis `CallSid` persistence |
| Outbound dialer *(new component)* | — | — | `campaigns.py` (dial-queue: enqueue/claim/record) + `dialer_worker.py` (background loop, DND + calling-hours gated) + `pipeline/outbound_dialer.py` (Twilio REST origination) |
| Human handoff / warm transfer | — | EscalationHub + browser text-relay (Phase A) | **+ `pipeline/warm_transfer.py`** — real PSTN handoff via Twilio's Conference API (Phase B), plus `end_twilio_call` for a REST-driven, orchestrator-initiated hangup |
| STT svc | Groq/Sarvam batch | + Deepgram streaming, per-bot mode | unchanged |
| NLP svc | batch `/v1/respond` | + NDJSON `/v1/respond/stream` | + `flow_gen.generate_flow` (auto flow-builder) |
| Platform (tenancy/admin) | — | Model-connection registry (BYOK), Users/org registry seam | **Real admin surface**: `/v1/orgs`, `/v1/users` (invite/accept/manage), Google Sign-In login route |

## 5. Technology stack (v1 → v2 → v3 → v4)

| Layer | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| STT | stub | Groq Whisper, Sarvam | + Deepgram (streaming) | unchanged |
| NLP | Groq | Groq 8B/70B | + streaming; BYOK LLM | + `flow_gen` (LLM-drafted conversation flow) |
| TTS | stub | Orpheus/Magpie/Sarvam(WS) | + response cache; BYOK TTS | unchanged |
| Embeddings | hash | NVIDIA | + FastEmbed local | + Bedrock Titan v2 (real boto3 impl, needs model-access grant) |
| Rate limit | in-mem | in-mem | + Redis | unchanged |
| Secrets | env | env | + encrypted BYOK store + SSRF guard | + boot-time warning (not hard-fail) if `KOEL_SECRET_KEY` unset in production |
| FE | monolith SPA | monolith + mic/VAD | decomposed + code-split + streaming captions | + AdminView + auto flow-builder UI |
| Telephony | — | — | ingest seam | **full transport**: `TelephonyCallSession` + `TwilioMediaBridge`, DTMF, idle-frame watchdog |
| Outbound/carrier origination | — | — | — | Twilio REST Calls API origination + Conference-API warm transfer |
| Auth | password JWT | + refresh | + persistence, org scoping | + Google Sign-In (JWKS-verified ID token, allowlist model) |
| AWS in-region (SageMaker/Bedrock) | — | — | design only | boto3 real code; Terraform written, **not applied** |

## 6. Key data flows

### 6.1 Outbound turn — v3 streaming happy path (browser)
Unchanged from v3: mic frames → streaming/buffered STT → `handle_final_transcript`
→ streaming NLP + cached/streamed TTS → audio back, with O-5 speculative RAG
possibly pre-fetched on the endpointing hold. Falls back to the buffered path
on any streaming failure.

### 6.2 Resilience/degradation
Unchanged degrade paths from v3, plus telephony's own: Twilio frame idle
(30s, no carrier frame at all) → treated like an abnormal socket drop, the
call is marked `call_dropped` rather than left `in_progress`; a malformed
carrier frame is logged and skipped, never crashes the call; speculative RAG
divergence/timeout → NLP retrieves normally (identical logic to browser, now
literally the same class).

### 6.3 Telephony turn — v4 full-parity flow *(new)*
Twilio Media Streams `media` frame (8kHz µ-law) → decoded to 16kHz PCM16 →
(per-bot ingest mode) buffered RMS-endpointed or Deepgram-streaming STT →
Gate 1 barge-in check (RMS voice-activity for the buffered path, the STT
partial itself for the streaming path) feeding the **same** `BargeInClassifier`
Gate 2 the browser uses → smart-turn hold (`SmartTurnAnalyzer`) may merge a
follow-up fragment instead of answering mid-thought, firing
`SpeculativeRagCoordinator.start()` during the hold → on resolve,
`orchestrator.handle_final_transcript` runs unchanged (transport-blind) →
reply frames transcoded PCM16→µ-law and played, with a confirmed barge-in
sending a Twilio `clear` frame. Every turn also buffers both the bot's and
the customer's audio for the bidirectional call recording, and updates the
no-input watchdog's clock. At call end: `persist_call` writes the call to
Postgres (previously never happened for telephony at all), and
`dispatch_followup`/`summarize_and_annotate_crm` (shared `postcall.py`) fire
off the critical path.

### 6.4 Outbound origination flow *(new)*
`dialer_worker.run_dialer_loop` polls for due `DialAttempt` rows (claimed via
`FOR UPDATE SKIP LOCKED`), skips anything DND-scrubbed or outside RBI calling
hours, then `outbound_dialer.originate_call` places the PSTN call via
Twilio's REST Calls API pointed at `/twilio/stream?attempt_id=...&bot_id=...`
— from there it's the same 6.3 flow. A Twilio status-callback webhook reports
back legs that never even reach the Stream (no-answer/busy/failed/canceled)
so `record_dial_result` can apply the retry policy.

## 7. Cross-cutting concerns

| Concern | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| PII egress | redact before TTS/logs | same | + TTS-cache TTL bounds derived-audio PII retention | unchanged posture; bidirectional recording adds no new egress path (same S3-upload gate, no-op without a bucket) |
| Secrets | env only | env only | BYOK secrets encrypted at rest; SSRF guard | + boot warns (not hard-fails) if `KOEL_SECRET_KEY` unset in production |
| Concurrency | single worker | single worker | Redis limiter + serialized streaming finals + pool observability | + outbound dialer's `FOR UPDATE SKIP LOCKED` claim; cross-process escalation/monitor fan-out via Redis pub/sub (previously single-process only) |
| Tenancy | soft | soft (org_id seam) | enforced org scoping at login + per-row | + real org/user admin flow (platform vs tenant-scoped gating). **New accepted risk**: cross-tenant username/email enumeration (narrow, admin-gated, rate-limited) |
| **Call recording** *(new)* | — | — | bot audio only, either transport, S3-gated | **Bidirectional** — bot AND customer audio, on both transports; same S3-upload gate (no-op without a bucket) |

## 8. Deployment view
Unchanged base (Render, `koel.combined:app` from `main`, self-migrating
Alembic, keep-alive). v4: **`render.yaml` reconciled** with the live Render
services (deploy-config drift fixed); **`alembic upgrade head` now runs
unconditionally on every boot**, gating only the expensive full seed behind
the empty-DB check (previously migrations rode the same gate as seeding, so
a schema change without a fresh empty DB could go un-migrated). Known drift
to reconcile: the backend and frontend Render services were, at last check,
tracking different git branches — confirm and fix, it's an oversight, not a
design choice.

## 9. Compliance & security posture

| Item | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| DPDP data-minimisation | design | redaction | + bounded TTS-cache retention; per-tenant isolation | unchanged posture; bidirectional recording doesn't change the redaction/consent model |
| TRAI opt-out / DND | design | enforced in turn loop | preserved across streaming path | + DND scrub now also gates the **outbound dialer** before every origination attempt (previously only the inbound flow enforced it) |
| Audit trail | design | per-turn | partial-turn recorded even on cancellation | + telephony calls are now actually **persisted to Postgres** (previously invisible to call history/analytics/QA scoring entirely) |
| SSRF | n/a | n/a | guard on all BYOK provider URLs | unchanged |
| **Cross-tenant enumeration** *(new)* | — | — | — | **New accepted risk**: `POST /v1/users/invite` leaks a global-uniqueness existence bit (requires `role=admin` auth, rate-limited, no pre-auth path) — signed off, tracked in `SECURITY.md`, not an oversight |
| **Data residency** | design | design | 🔴 open — hosted US inference; Sarvam partial mitigation | **Unchanged, still 🔴 open — the single biggest compliance gap.** Bedrock embedder code being real is implementation readiness only, not a status change; needs a legal ruling before scaling real customer PII through the US-hosted path |

## 10. Scalability & resilience
v4 extends multi-worker correctness to escalation/monitor state — Redis
pub/sub fan-out (dormant without a `redis_client`, matching every other
cross-instance feature in this codebase) — and adds the outbound dialer's
own concurrency-safe claim (`FOR UPDATE SKIP LOCKED` on Postgres; SQLite
degrades to a windowed select+update for single-worker dev/test). Telephony
is now a fully independent failure domain with its own degrade paths
(idle-frame timeout → abnormal-disconnect persistence, no-input watchdog
poll) mirroring the browser transport's guarantees, rather than merely
ingesting audio into it.

## 11. Current state — real vs. stub (v1 → v2 → v3 → v4)

| Capability | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| NLP | real Groq | + language-routed | + streaming + BYOK LLM | unchanged; + auto flow-builder (`flow_gen.generate_flow`) |
| RAG | pgvector + hash stub | + NVIDIA embedder | + local FastEmbed + speculative | + Bedrock Titan v2 embedder now real (needs AWS model-access grant); O-5 extracted to shared `SpeculativeRagCoordinator` |
| STT | stub | Groq+Sarvam batch | + Deepgram streaming + per-bot mode | unchanged |
| TTS | stub | 3 engines + WS | + cache + BYOK TTS | unchanged |
| Endpointing | fixed timer | + smart-turn v3.2 | drives O-5 speculation (browser only) | drives O-5 speculation on **both** transports now |
| Barge-in | design | tested live | unchanged across streaming path | + telephony parity: RMS Gate 1 (buffered ingest) or STT-partial Gate 1 (streaming ingest), both feeding the shared Gate 2 classifier |
| Rate limit | in-mem | in-mem | Redis (real, multi-worker) | unchanged |
| Auth/tenancy | JWT | +refresh (FE gap) | persisted + refresh + enforced org scoping | + Google Sign-In (allowlist); + real org/user invite-accept-manage admin flow |
| Human handoff | — | — | real (EscalationHub + browser text-relay + monitor) | + real PSTN warm transfer (Twilio Conference API); telephony escalation registration now correctly **gated** (previously the bot talked over a customer already on hold) |
| **Telephony** | — | browser WS only | **ingest seam only** — no call-handling policy, bridges "the next build" | **Fully built, feature-parity transport** — `TelephonyCallSession` drives barge-in, hold+speculative RAG, no-input watchdog, gated escalation, REST hangup, idle-frame watchdog, DTMF, Postgres persistence |
| **Outbound dialer** *(new)* | out of scope | out of scope | out of scope | **Built**: campaign queue + Twilio-origination worker, DND + calling-hours gated |
| **Call recording** *(new)* | — | — | bot audio only, either transport | **Bidirectional** — bot AND customer audio, both transports |
| **AWS in-region providers** *(new)* | — | — | stubs / `NotImplementedError` | **Boto3 code real** (SageMaker Whisper/Orpheus, Bedrock Titan) — no endpoint deployed; Terraform written, not applied |
| Frontend | type-to-talk | mic+VAD monolith | decomposed + code-split + streaming captions | + AdminView (org/user mgmt) + auto flow-builder UI |

## 12. Risks & future work
See [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md) (now a stale snapshot —
cross-check against source before trusting any "still to build" item; the
outbound dialer, PSTN warm transfer, Twilio bridge, and BYOK connections it
lists as future work are all built as of v4). Headline remaining, in rough
priority order: **(1) data-residency legal ruling** — unchanged, still the
single biggest open item, gates whether the US-hosted inference architecture
is acceptable to keep; **(2) rotate the 5 credentials** pasted in plaintext
during development (Groq, NVIDIA, Sarvam, Deepgram, Render API token — see
`SECURITY.md`); **(3) resolve the GitHub Actions billing block** so CI runs
again (currently failing in ~3s with zero steps, not a code issue);
**(4) reconcile backend/frontend branch drift** on Render; live Twilio/Exotel
production account + DID still needed before any of the built telephony/
dialer code carries real traffic; SageMaker/Bedrock endpoints remain
undeployed (Terraform written, `apply` never run); Exotel (1.4) is
comparatively less exercised than Twilio and its signature scheme is an
explicitly-flagged placeholder pending confirmation against live docs.
Module-level detail: [LLD_V4.md](./LLD_V4.md).
