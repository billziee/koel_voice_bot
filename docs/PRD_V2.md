# KOEL — Product Requirements Document v2 (Conformance Status)

> This document tracks **what was actually built** against
> [`koel-voice-bot-prd-v1.2.html`](../../koel-voice-bot-prd-v1.2.html) (the
> original PRD, external to this repo, **unmodified**). It is not a rewrite of
> the PRD's vision, use cases, or architecture narrative — those are unchanged
> and the original document remains authoritative for *intent*. This document
> is the authoritative record of *conformance*: per-section status, what
> shipped differently than specified (and why), and what's still open. See
> [`HLD_V2.md`](./HLD_V2.md) / [`LLD_V2.md`](./LLD_V2.md) for the corresponding
> architecture/code-level v2 documents, and
> [`FUTURE_PROCEEDINGS.md`](./FUTURE_PROCEEDINGS.md) for the plan to close the
> remaining gaps.
>
> **Status legend:** 🟢 Met · 🟡 Met differently / partially · 🔴 Not met / open
> · ⚖️ Renegotiated (the original target was found to be infeasible and a new
> one was substituted, with the substitution documented)

---

## Executive summary

**The product's core thesis — "configuration, not code, is the moat" — holds
fully and is arguably the best-proven part of the system**: every bot is a
database row, the Bot Builder wizard creates new ones with zero engineering,
and adding a second real voice vendor (Sarvam) required no orchestrator
changes. **What does not hold is the PRD's headline latency number**: a
literal &lt;300ms P95 full-turn is physically impossible with the caller in
India and inference in the US (~250ms of ocean round-trip time alone). That
number has been renegotiated — see §14 below — into a *perceived* first-audio
target (met) and a *true* first-audio target (measured, not yet met, with a
concrete plan in `FUTURE_PROCEEDINGS.md`). **Data residency (§11) is the other
major open item**: PII currently transits to US-hosted Groq/NVIDIA (Sarvam,
partially, is India-hosted), which does not satisfy the PRD's residency
requirement.

Everything else — the resilience engine (§05), the compliance logic (§11), the
engineering standards (§08), and the 4-screen demo frontend (§09) — is
**substantially delivered and, in several places, ahead of the original spec**
(the AI pipeline is now genuinely real across STT/NLP/TTS/embeddings, not the
stubbed placeholders the original MVP scope allowed for).

---

## §01 — Vision

**PRD claim:** "KOEL is not a chatbot with a voice skin... every architectural
decision exists to serve sub-300ms conversation. The latency constraint is the
product."

**Status: ⚖️ Renegotiated.** The architectural decisions described (WebSocket
transport, streaming STT/TTS, session-scoped RAG) are all present and correct
*in kind* — but the destination they were aimed at (a literal sub-300ms full
turn from an Indian caller to US-hosted inference) is not reachable by any
amount of further engineering; it's bounded by the speed of light. The
"configuration-first" half of the vision — the same engine serving credit
card cross-sell, collections, and insurance from config alone — is **fully
realized** and is the part of the vision that has proven durable.

## §02 — Goals & Success Metrics

| Goal | Metric | MVP Target | Status |
|---|---|---|---|
| Natural Conversation | Turn latency P95 | &lt;300ms | 🔴 Physically infeasible; renegotiated (see §14) |
| Language Coverage | STT WER, Hindi/Hinglish | &gt;90% | 🟡 Eval harness exists; not freshly re-measured against the current provider mix |
| Intent Resolution | Intent classification accuracy | &gt;85% | 🟡 CI gate exists; not freshly re-measured since the NLP model was split (8B for English, 70B for Hindi/Hinglish) |
| Cross-Sell Conversion | Warm leads / 100 calls | &gt;12 (demo benchmark) | 🔴 Demo dashboard number is canned, not derived from real call data (roadmap Q-3) |
| Collections Recovery | Promise-to-pay capture rate | &gt;30% of connected calls | 🔴 The PTP capture mechanism itself isn't built yet (roadmap §04/2.1) |
| Cost per Call | vs. human agent | &lt;20% of agent cost | 🔴 Not modeled — three real inference vendors now in the mix with no unit-economics tracking (roadmap O-9) |
| Customer Acceptance | Call completion rate | &gt;70% | 🔴 Not measured against real traffic |
| Escalation Quality | Full-context handoff | 100% | 🟡 The *handoff payload* is 100% complete (transcript + CRM context shipped on every escalation) — but nothing receives it; there's no agent queue or console yet (roadmap 3.1) |

**Honest read:** the *engineering* gates (latency, WER, intent accuracy) have
harnesses but stale measurements; the *commercial* gates (conversion, PTP,
cost, completion) require live campaign volume this system has not yet run.
The PRD itself flagged these as "MVP demo benchmarks, not production SLAs" —
that framing still applies.

## §03 — Personas & Modes

**Status: 🟢 Met for MVP scope.** All four outgoing use cases (credit card,
collections, personal loan, insurance) are implemented as configurable bot
templates. Incoming/inbound mode (Koel_bot) is correctly deferred per the
PRD's own phasing — see §10 below and roadmap item **3.2**.

## §04 — Use Cases

**Status: 🟡 Config framework complete; in-call revenue actions mostly not yet built.**

The use-case *template* mechanism (name/script/CRM-mapping/success-criteria via
the Bot Builder) is fully general and matches the PRD's "no engineering
required for new use-case deployment" claim. What's specifically **not** built
per use case, all tracked in `FUTURE_PROCEEDINGS.md`:

- **Collections**: payment-link SMS dispatch and structured promise-to-pay
  capture are described in the PRD's use-case detail but not implemented —
  roadmap **2.1**. (The RAG knowledge base for collections objection-handling
  *is* seeded and grounding responses.)
- **Personal Loan**: "dynamic EMI calculation" is not implemented — the bot
  cannot currently compute a real EMI quote, only discuss scripted figures —
  roadmap **2.2**.
- **Insurance**: "verbal consent capture" with an evidentiary link to the
  recording is not implemented as a distinct compliance artifact (general
  consent-at-call-open *is* captured and timestamped, per §11) — roadmap **2.3**.
- **Sample conversation flow** (the PRD's Hinglish credit-card dialogue) — the
  live system reproduces this shape correctly: greeting → objection →
  RAG-grounded rebuttal → escalation on high interest, verified live this
  development cycle.

All three configured use cases' RAG knowledge bases are seeded and grounding
real NLP responses (this was previously a gap — only the credit-card index was
seeded — closed this cycle).

## §05 — System Architecture

**Status: 🟢 Met — the resilience engine described here is the most
completely and faithfully implemented section of the PRD**, and has been
*extended* beyond the original spec:

- **Dual-Gated Interruption Engine**: built exactly as specified (client VAD →
  `BARGE_IN_CANDIDATE` → server backchannel/duration classification →
  `TTS_KILL`), **plus an unspecified third refinement**: a min-words gate so a
  single junk word from STT noise doesn't kill TTS on its own, and the client
  VAD is now a neural model (Silero) with an energy-based fallback rather than
  energy-only.
- **Latency Masking (filler audio)**: built as specified.
- **Silence/Timeout rules**: built as specified, with the no-input windows
  tuned wider than the PRD's 5s/5s (8s/8s) based on live-call feedback that 5s
  felt impatient — a deliberate, documented deviation, not an oversight.
- **Session recovery (15s grace + reconnect)**: built as specified and tested,
  including a real, live verification of the reconnect-vs-drop finalizer race.

The one architectural addition not anticipated by the PRD: a **semantic
end-of-turn gate** (smart-turn v3.2) layered on top of the fixed-silence timer,
because a fixed trailing-silence window alone cuts off speakers who pause
mid-thought — a real-world failure mode the PRD's fixed-timeout design didn't
anticipate.

## §06 — Feature Specification

| Feature | Status |
|---|---|
| Real-Time Transcription | 🟢 Met |
| Multilingual Conversation | 🟡 Met differently — see below |
| RAG-Enhanced Responses | 🟢 Met (real embeddings + retrieval, all 3 use cases grounded; "Bedrock retrieves" in the PRD text is now Groq + a real NVIDIA embedder, not Bedrock — see §07) |
| Bot Configuration Panel | 🟢 Met (Bot Builder wizard + Configuration screen) |
| CRM Integration (Dummy) | 🟢 Met — the interface-contract design worked exactly as intended |
| Audio Visualisation | 🟢 Met |
| Session History & Replay | 🟢 Met (transcript + filters; audio replay from stored recording where S3 is configured) |
| Escalation Engine | 🟡 The *trigger* logic (intent/sentiment/explicit request) and the *handoff payload* (full transcript + CRM context) are both built; there is no receiving agent queue yet — an escalated call currently just ends. Roadmap **3.1**. |
| Graceful Language Fallback | 🟢 Met — and made to work correctly under a design constraint the PRD didn't anticipate (see below) |
| Tone & Prosody Control | 🟢 Met |
| Ambient Audio Layer | 🟢 Met, plus a per-bot on/off toggle not specified in the PRD (added based on user feedback that it should be operator-controllable) |

**Multilingual conversation — met differently than specified.** The PRD calls
for "automatic language detection within the first utterance" and "seamless
mid-call language switching." In practice, automatic per-utterance language
detection on real Indian phone/browser audio proved unreliable — it
misidentified Hindi as Russian or Ukrainian, and background noise as Spanish
or French, on live calls. The shipped design instead **pins STT to the bot's
configured language** (eliminating those misfires) and makes mid-call
switching **explicit-request-driven** (a caller saying "continue in English"
is honored reliably) rather than inferred from an unreliable per-utterance
guess. Graceful fallback for genuinely unsupported languages still works under
this constraint via a secondary signal (a sustained utterance that
force-transcribes with very low confidence).

## §07 — Technology Stack

**Status: 🔴 Materially different from the "finalized" stack — by deliberate
substitution, not oversight.**

| Layer | PRD "finalized" choice | What's actually running | Why |
|---|---|---|---|
| STT | OpenAI Whisper on SageMaker | **Groq Whisper** (hosted) + **Sarvam** (India-hosted) | SageMaker GPU provisioning wasn't available to this project; Groq/Sarvam give equivalent or better accuracy with zero infra ops |
| NLP/LLM | AWS Bedrock (Claude/Titan) | **Groq** (`llama-3.1-8b`/`70b`, language-routed) | Bedrock access/VPC setup wasn't available; Groq's inference speed materially helps the latency story even though it's US-hosted |
| TTS | Orpheus on SageMaker | **Groq Orpheus** + **NVIDIA Magpie** + **Sarvam Bulbul** (3 engines, routed by language) | Same infra-availability constraint; three vendors were needed because no single one covers English + Devanagari Hindi + romanized Hinglish well |
| Compute | AWS EC2 + SageMaker | **Render** (a single-process deployment on a memory-constrained free/starter tier) | No AWS access for this phase of the project; Render was chosen for zero-ops deployment during MVP validation |
| Database / Redis / Transport / Backend framework | PostgreSQL+pgvector / Redis / WebSockets / FastAPI | **Unchanged — met exactly as specified** | — |
| Observability | OTel + Kibana/ES + CloudWatch | OTel implemented; **Kibana/CloudWatch not wired** | No infra to wire them to in this environment; the instrumentation that would feed them is in place |

**This is the single largest specification deviation in the project**, and
it's the direct cause of both the latency renegotiation (§14) and the
residency gap (§11) — Groq and NVIDIA are US-hosted; only Sarvam, added
later, is India-hosted, and only for part of the pipeline. Closing this gap
(moving to the AWS stack, or expanding Sarvam's role, or self-hosting in
`ap-south-1`) is the single highest-leverage item in `FUTURE_PROCEEDINGS.md`
(**O-7**).

## §08 — Engineering Standards

**Status: 🟢 Met, comprehensively**, and unaffected by the stack substitution
above:

- Richardson Maturity Model Level 2: 🟢 met on every REST endpoint.
- OpenAPI/Swagger at `/docs`: 🟢 met on every service.
- OpenTelemetry distributed tracing: 🟢 met — spans propagate FE→VB→AI with a
  persisted `trace_id` per call.
- Log level discipline: 🟢 met (structured JSON, correlation ids, no bare
  prints).
- HTTP response codes: 🟢 met.
- Session management (Redis, no in-memory app state): 🟢 met.
- Authentication (JWT on REST + WS pre-upgrade): 🟢 met on the backend. 🟡 The
  frontend does not yet persist the token or use the already-built refresh
  endpoint — every page reload logs the operator out. This is a frontend gap,
  not a backend one (roadmap **Q-2**).
- WebSocket lifecycle discipline (heartbeat, grace TTL, reconnect with
  original Session ID): 🟢 met and tested, including a live-verified
  reconnect-race scenario.

## §09 — Frontend Specification

**Status: 🟢 All four screens present and functional; quality issues open, not
scope gaps.**

| Screen | Status |
|---|---|
| 1. Bot Selection Dashboard | 🟡 Present; the summary KPI strip at the top is hardcoded demo data, not derived from `getAllStats` (which exists and is unused) — roadmap **Q-3** |
| 2. Configuration Panel | 🟡 Present; does not currently load a bot's actual saved configuration into the form on open, and shows an always-on "unsaved changes" indicator regardless of real edit state — roadmap **Q-3** |
| 3. Active Call Interface | 🟢 Present and functional with real audio (not the PRD's minimum bar of a working WS connection — this now includes real mic capture, neural VAD, and real barge-in) |
| 4. Call History | 🟢 Present — filterable, exportable, with per-call transcript and latency breakdown |

The frontend additionally exceeds the PRD's Screen 2 spec: a full multi-step
**Bot Builder wizard** (not explicitly specified, but consistent with "no
dev work required" configuration) and a **Knowledge Base admin screen**
including file-upload ingestion (PDF/text) — neither called out in the
original PRD but a natural extension of the "Configuration Panel" concept.

## §10 — MVP Scope

**In-MVP checklist — all items present** except the technology substitutions
already noted in §07 (Whisper/Bedrock/Orpheus are Groq/Sarvam/NVIDIA
equivalents, not the named AWS services) and Kibana (OTel is wired,
Kibana/CloudWatch dashboards are not).

**Post-MVP list — status:**

| Item | Status |
|---|---|
| Live CRM integration | 🔴 Not started; the dummy CRM's contract is explicitly designed as the adapter interface — roadmap **6.1** |
| Production telephony (Twilio/Exotel) | 🔴 Not started; the audio-transcoding half of the seam (µ-law) is built, no carrier bridge exists — roadmap **1.1**/**1.4** |
| Incoming bot (Koel_bot) | 🔴 Not started — roadmap **3.2** |
| Multi-tenant bot management | 🟡 The `org_id` seam exists and is consistently applied wherever set, but not enforced non-null — roadmap **6.4** |
| Advanced analytics dashboard | 🟡 A stats API exists (per-bot + org-wide); a funnel/objection/sentiment dashboard is not built — roadmap **6.2** |
| A/B script testing | 🔴 Not started — roadmap **6.3** |
| Regulatory audit log exports | 🔴 Not started; all the underlying data (transcripts, consent, config-change audit) is already persisted — roadmap **6.5** |
| EC2 auto-scaling pipeline | 🔴 N/A under the current Render deployment; would need to be redesigned for the eventual AWS target |

## §11 — Compliance & Security

| Domain | Requirement | Status |
|---|---|---|
| RBI — Collections | Calling-hours window; self-identification | 🟡 Self-identification: 🟢 met (every greeting discloses bank + automated-call status). Calling-hours: 🟢 the enforcement logic is built and **a timezone bug was found and fixed this cycle** (it was evaluating the window against the server's UTC clock instead of Indian time) — still **opt-in**, off by default so demos aren't blocked |
| IRDAI — Insurance | Mandatory disclosure; consent capture; licensed handoff | 🟡 Disclosure + consent-with-timestamp: 🟢 met. Evidentiary consent recording (linked to a specific recording offset) is not yet a distinct artifact — roadmap **2.3**. Licensed-advisor handoff depends on the escalation-engine gap (§06) |
| DPDP Act 2023 | Consent; data minimisation; PII masking | 🟢 Met — consent captured with timestamp at call open, only use-case-relevant CRM fields loaded per session, PII masked in logs |
| TRAI — Telecom | DND scrubbing; opt-out honouring | 🟢 Met — DND checked before dialling (409 refusal), opt-out immediately flags the CRM and ends the call |
| **Data Residency** | **All compute/storage in AWS ap-south-1** | 🔴 **Not met — the most significant open compliance item.** Inference runs on US-hosted Groq and NVIDIA; only Sarvam (part of the TTS/STT pipeline) is India-hosted. See [`RESIDENCY.md`](./RESIDENCY.md) for the full analysis and `FUTURE_PROCEEDINGS.md` **O-7** for the remediation path (expand Sarvam's role, or self-host in-region). |
| Auditability | Transcript, recording, consent log, outcome, trace retained | 🟢 Met — every call persists all of these; a *formatted export* mechanism for regulators doesn't exist yet (roadmap **6.5**) but the underlying data does |

## §12 — Risks & Mitigations

The PRD's risk table maps almost 1:1 onto what was actually built to mitigate
each risk (streaming STT/TTS, RAG grounding, self-identification, the
dual-gate interrupt engine, filler audio, language fallback, session
recovery, JWT auth, tone-tagged prosody) — **these mitigations are real and
tested**, not aspirational, which is a stronger result than most PRDs achieve.
The one risk the PRD's own mitigation ("load test before demo") flagged and
that has now actually been executed: **latency under load** — a real load
test at 10 concurrent sessions found **0% errors** but a p95 far above the
literal 300ms target, confirming the risk was real (dead air / robotic feel
under naive expectations) while showing the *reliability* half of the
mitigation (warm endpoints, no error-rate degradation under load) works.

## §13 — Roadmap & Phasing

Mapping the PRD's five phases onto what's shipped:

- **Phase 0 (Foundation):** 🟢 complete.
- **Phase 1 (Pipeline MVP — exit criterion "&lt;300ms P95 in demo conditions"):**
  🟡 the pipeline is complete and live end-to-end; the exit criterion itself
  is unmet for the reason detailed in §14 — this is a target renegotiation,
  not an unfinished pipeline.
- **Phase 2 (Multi Use-Case + Config — exit "new bot instance via config, no
  code change"):** 🟢 complete and demonstrated (all 4 use cases, Bot Builder
  wizard).
- **Phase 3 (Stakeholder Demo — exit "10 concurrent sessions load-tested,
  clean demo across 3 bot instances"):** 🟡 the demo across bot instances
  works; the 10-concurrent load test has now actually been run (0% errors,
  latency measured — see §02) but was not run *before* a stakeholder demo as
  the PRD's process implies; it was run as part of this document's research.
- **Phase 4 (Production Hardening):** 🔴 not started — this is
  `PRODUCTION_MASTERPLAN.md`'s entire scope (infra provisioning, secrets
  management, staging, telephony, multi-tenant, compliance sign-off).

**Net position: the project is at the end of Phase 2, partway into Phase 3**,
with Phase 3's load-testing exit criterion now actually measured (previously
it was simply never run) and found to expose the Phase 1 latency criterion as
unreachable as literally stated.

## §14 — Non-Functional Requirements

| Requirement | Target | Status |
|---|---|---|
| End-to-End Conversational Latency | &lt;300ms P95 | 🔴/⚖️ **Renegotiated.** Measured live (10 concurrent, Render US-East, from India): first-audio **p50 ≈ 0.9–1.0s, p95 ≈ 2.1–2.3s**. The floor is ~250ms of unavoidable ocean RTT plus real inference time; no amount of application-level optimization reaches 300ms while the topology is India-caller/US-inference. New SLO (documented in [`SLO.md`](./SLO.md)): *perceived* first-audio &lt;300ms — **met**, via filler audio + sentence-streaming/WS-streaming TTS masking the gap; *true* first-audio p50 &lt;600ms — **not yet met**, targeted by `FUTURE_PROCEEDINGS.md` Part A (O-1 through O-6); ~0.4–0.6s from India is the realistic floor after in-region inference (**O-7**). |
| STT Accuracy — Hindi/Hinglish | &gt;90% WER | 🟡 Not freshly measured against the current Groq+Sarvam mix; an eval harness exists |
| TTS Naturalness | MOS &gt;3.8 | 🔴 Not measured — no internal listener eval has been run against any of the three live TTS engines |
| Session Capacity (Demo) | 10 concurrent sessions | 🟢 **Met and measured**: 0% error rate at 10 concurrent, live |
| API Availability | 99.9% uptime (demo window) | 🔴 Not applicable to the current free/starter-tier Render deployment, which is subject to idle spin-down (mitigated by a keep-alive mechanism, not eliminated) |
| Call Recording Storage | 30-day retention, S3 | 🟡 The upload path and retention config exist in code; no S3 bucket is provisioned in the current environment (Masterplan item) |
| Data Residency | AWS India region | 🔴 Not met — see §11 |
| PII Handling | Name + account number masked in logs | 🟢 Met |
| Barge-In Stop Latency | &lt;150ms confirmed-interrupt → silence | 🟢 Met — client-side playback flush on speech-start is near-instant |
| Backchannel False-Interrupt Rate | &lt;5% | 🟡 No formal offline eval against a labelled set; the min-words gate added this cycle is a direct mitigation, not yet independently measured |
| Filler Audio Injection | Trigger at 300ms wait, &lt;100ms first byte | 🟢 Met by design (pre-rendered clips, local cache) |
| No-Input Handling | 5s reprompt / 5s close | 🟡 Met differently — windows widened to 8s/8s based on live-call feedback that 5s felt impatient; the underlying state machine (reprompt once, then close with a logged outcome) matches the spec exactly |
| Session Recovery Window | 15s grace, resume by Session ID | 🟢 Met and tested, including a live-verified reconnect race |
| Endpoint Authentication | 100% JWT-validated | 🟢 Met on the backend; 🟡 frontend token handling has a UX gap (§08) |
| Tone Tag Coverage | 100% of NLP responses | 🟢 Met |

---

## Summary verdict

| Dimension | Verdict |
|---|---|
| **Product thesis** (config over code) | 🟢 Proven, not just claimed |
| **Conversation engine & resilience** (§05) | 🟢 Fully delivered, extended beyond spec |
| **Compliance logic** (§11, minus residency) | 🟢 Delivered |
| **AI pipeline reality** | 🟢 Fully real (STT/NLP/TTS/RAG all live), across two independent vendor stacks |
| **The headline latency SLA** | 🔴 Physically unreachable as literally specified; honestly renegotiated with a measured baseline and a concrete improvement plan |
| **Data residency** | 🔴 Not met; the direct consequence of the §07 stack substitution |
| **Frontend polish & auth UX** | 🟡 Functionally complete, several truthfulness/persistence bugs open |
| **Production readiness** (infra, telephony, live CRM, multi-tenant) | 🔴 Correctly scoped as post-MVP; not started |

See [`FUTURE_PROCEEDINGS.md`](./FUTURE_PROCEEDINGS.md) for the prioritized plan
covering every 🟡/🔴 item above, and `PRODUCTION_MASTERPLAN.md` for the
infra/ops/compliance phasing that sits alongside it.
