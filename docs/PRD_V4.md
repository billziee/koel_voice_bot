# KOEL — Product Requirements Document v4 (Conformance Status)

> Supersedes [PRD_V3.md](./PRD_V3.md). Tracks product conformance across the four
> build eras. v4 = after telephony/PSTN calling reached full feature parity with
> the browser transport (barge-in, smart-turn hold, speculative RAG, gated
> escalation, bidirectional recording), the outbound dialer + PSTN warm transfer,
> multi-tenant org/user administration + Google sign-in, the auto flow-builder,
> and four rounds of security/correctness bug-hunt fixes. Companions:
> [HLD_V4.md](./HLD_V4.md), [LLD_V4.md](./LLD_V4.md).

## Conformance at a glance (v1 → v2 → v3 → v4)

| PRD area | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Multilingual EN/HI/Hinglish | stub voices | real (Orpheus/Magpie/Sarvam), script-correct | unchanged + per-bot BYOK voice | unchanged |
| STT | type-to-talk stub | Groq Whisper (pinned) + Sarvam | + Deepgram streaming (interim partials) + per-bot chunk\|ws | unchanged |
| NLP intent/response/tone | real Groq | language-routed 8B/70B | + first-token streaming; sentiment-adaptive tone (4.3) | unchanged |
| RAG grounding | hash stub | NVIDIA embedder + 3 seeded KBs + upload | + local FastEmbed + speculative retrieval | unchanged |
| Perceived latency | thesis | filler + sentence-streaming TTS | token-bound first-audio (O-1/O-2/O-6) | unchanged |
| Barge-in / endpointing | design | dual-gate + smart-turn v3.2, tested | preserved across the streaming path | **+ telephony parity: RMS-based Gate 1 for the buffered-ingest path, shared Gate 2 classifier reused as-is, smart-turn hold + speculative RAG now run on PSTN calls too** |
| Compliance (TRAI/DPDP) | design | enforced in the turn loop | preserved on streaming; bounded TTS-cache PII; enforced org isolation | **+ 4 rounds bug-hunt: cross-tenant IDOR fix, WS finalize/data-loss cluster, telephony frame-parse hardening, Twilio signature-verification fix** |
| CRM read/flag/summary | endpoints | + post-call summary | summaries surfaced in history/dashboard (5.3) | **+ Bot Builder Step 4 (Knowledge & CRM) wired to live backend data (was mocked)** |
| Dashboard | full SPA (type-to-talk) | mic + VAD | decomposed + code-split; live KPIs (Q-3); connections manager; flow editor (6.6) | **+ admin panel (org/user provisioning, invites); auto flow-builder drafts a flow from KB chunks for the operator to refine (6.6)** |
| Auth | JWT | +refresh (FE gap) | persisted + silent refresh (Q-2); real Users + org scoping (6.4) | **+ Sign in with Google (allowlist model); org/user admin API (invite/accept/manage) + AdminView UI for self-service tenant onboarding** |
| Human escalation | design | design | live agent handoff (3.1) + supervisor monitor (3.3) | **+ PSTN warm transfer (Twilio Conference: redirect customer leg + whisper-then-join operator leg); telephony escalation registration now gated correctly — previously the bot kept talking over a customer already on hold** |
| Inbound | out of scope | out of scope | inbound bot (3.2) | unchanged |
| Cost governance | none | none | per-call economics + budget caps (O-9) | unchanged |
| Extensibility | env-fixed providers | env-fixed | BYOK per-bot LLM/TTS/STT connections | unchanged |
| Telephony / PSTN calling | out of scope | out of scope | ingest seam only (Twilio bridge scaffolding, no call-handling policy) | **full transport parity with the browser: `TelephonyCallSession` drives barge-in, smart-turn hold, speculative RAG, gated human-handoff, a no-input watchdog, a REST-driven hangup for orchestrator-initiated endings, and bidirectional call recording (bot AND customer audio — previously only the bot's audio was ever recorded, on either transport); calls are now actually persisted to Postgres at all (previously they weren't)** |
| Outbound calling / campaigns | out of scope | out of scope | out of scope | **campaign dial-queue (claim/retry policy, `FOR UPDATE SKIP LOCKED`) + a dialer worker that originates calls via Twilio's REST API and feeds outcomes back into the retry policy — dormant until Twilio is configured** |
| In-region AWS infra (SageMaker/Bedrock/Terraform) | out of scope | out of scope | design only (O-7, legal-gated) | **Bedrock Titan v2 embedder is a real implementation (needs a Bedrock model-access grant in ap-south-1 to use); SageMaker Whisper/Orpheus providers are real boto3 code (no endpoint deployed — deliberately not faked); Terraform for the full ap-south-1 stack (S3, RDS, ElastiCache, IAM, ECS Fargate) is written and validated, `terraform apply` never run** |

## New product surfaces in v4
- **Telephony / PSTN calling, full parity:** phone calls now run the identical
  pipeline as browser calls end to end — see the Telephony row above.
- **Outbound dialer:** campaign dial-queue + Twilio-origination worker; PSTN
  warm transfer for human escalation via Twilio Conference.
- **Org/user administration:** admin-only invite/accept/manage API + AdminView
  UI; Google Sign-In (allowlist model) alongside password auth.
- **Auto flow-builder (6.6):** the LLM drafts a conversation flow (start node +
  branch-per-intent state machine) from a bot's KB chunks; the operator refines
  it in the existing flow editor rather than building one from a blank canvas.
- **AWS in-region groundwork:** Bedrock and SageMaker provider code is now real
  (not stubbed), and Terraform for the target ap-south-1 stack is written —
  neither is live yet (see the Terraform row and Non-conformance below).

## Non-conformance / deferred (see [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md))
In-region inference (O-7, legal-gated — **unchanged, still the open item**: NLP
and English STT/TTS still egress to the US; Bedrock/SageMaker provider code is
now real but nothing is actually deployed in-region, and this needs a legal
residency ruling before it's worth deploying); AWS Secrets Manager wiring
(Terraform has comments describing the ECS `secrets` block, not a working
integration); a live Twilio/Exotel production account + DID (the code path is
real and tested, there's no carrier account behind it); payments/WhatsApp/
scheduled-callbacks (Theme 2); advanced analytics (6.2). PSTN bridges are no
longer deferred — moved into the conformance table above as fully built.
Requirements unchanged from v3 except where the table above marks progress.
