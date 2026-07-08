# KOEL — Production Readiness Masterplan

> Status date: 2026-07-02 · Branch: `feat/groq-nlp-and-prd-hardening` (PR #1)
>
> **Where we are:** the codebase is production-*hardened* — real Groq/NVIDIA
> providers, resilience + compliance pipeline, 90% test coverage, a 24-agent
> adversarial audit's 13 findings fixed, verified live in **dev** (Docker +
> Postgres/pgvector + Redis). **Where we are not:** nothing is deployed to a real
> environment. This plan is the path from "hardened code" to "running, operated,
> compliant, scalable service."
>
> Legend: ✅ done · 🟡 partial · 🔴 gap · ⛔ launch blocker

---

## 1. Maturity by domain (16 domains)

| # | Domain | State | Headline gap |
|---|--------|-------|--------------|
| 1 | Application code & architecture | ✅ | Solid; backend is clean microservices |
| 2 | Frontend | 🔴 | `App.tsx` = 3,466 lines, `strict:false`, no state lib, no code-split |
| 3 | Infra & deployment (IaC, orchestration, CI/CD) | 🟡⛔ | Terraform written but **never applied**; no cluster, no staging |
| 4 | Data & persistence (RDS, migrations, backup/DR) | 🟡⛔ | Alembic ready; no managed DB, no backup/restore test |
| 5 | Secrets & key management | 🔴⛔ | Keys in `.env`; **exposed keys not rotated**; no secret manager |
| 6 | Compliance & data residency | 🔴⛔ | Hosted US inference ≠ DPDP/§11 ap-south-1; no legal sign-off |
| 7 | Observability & SLOs | 🟡 | Metrics/traces emitted; **no collector/Grafana/alerting wired** |
| 8 | Reliability, resilience & DR | 🟡 | In-code resilience good; no DR plan, no chaos test, single-worker rate-limit |
| 9 | AI/ML quality (STT/NLP/TTS/RAG) | 🟡 | Eval harnesses exist; **no gold-set run, no human MOS/WER baseline** |
| 10 | Telephony / real audio (PSTN) | 🔴 | µ-law seam only; no carrier bridge = still a browser demo |
| 11 | Performance & scale | 🟡 | Load script exists; **never run at target concurrency** |
| 12 | Testing & QA | ✅🟡 | 90% unit/integration; no E2E-in-CI, no UAT, no soak |
| 13 | Cost management | 🟡 | Token cost metric emitted; no budget alerts / quota caps live |
| 14 | Operations (runbooks, on-call) | 🔴 | No runbooks, no on-call, no incident process |
| 15 | Release management & rollout | 🔴 | No staging, no canary, no rollback drill, no feature flags |
| 16 | Documentation & handover | 🟡 | HLD/LLD + domain docs exist; no ops/runbook/API portal |
| 17 | India telecom onboarding (outbound) | 🔴⛔ | TCCCPR/DLT registration, header/consent scrubbing — a weeks–months legal/business process, **not started** |
| 18 | Unit economics / cost per call | 🔴 | Per-turn STT+LLM+TTS+embeddings cost never modeled; no per-call target or margin |
| 19 | Business completeness | 🟡 | Human-escalation handoff is a stub (no live agent pool); model-version pinning/upgrade-eval absent |

**Verdict:** engineering is ~80% there; **operations, deployment, compliance, and telecom onboarding are ~10% there.** The remaining work is mostly *not code* — provisioning, wiring, testing at scale, and organizational/legal sign-off. **Two of the blockers (residency, telecom onboarding) are legal/regulatory and can dominate the timeline regardless of engineering pace.**

---

## 2. ⛔ Launch blockers (must clear before ANY real traffic)

1. **Rotate the exposed Groq + NVIDIA keys** and move all secrets to a manager (AWS Secrets Manager / SSM). They were shared in plaintext → compromised.
2. **Data-residency decision (legal-first, likely forces in-region).** Hosted US Groq/NVIDIA does not meet PRD §11 / DPDP for Indian customer PII — and for a **financial-services** use case, **RBI's 2018 data-localization directive** requires payment-system data stored *only in India*. This is likely a hard prohibition on US egress, not a consentable risk. Treat option (b) — **self-host NIMs + Bedrock Titan in `ap-south-1`** — as the probable path, not the fallback; (a) a consented US pilot is only viable if legal confirms no payment data leaves and DPDP consent covers it. **Get the legal ruling before Phase 0 — it changes what you provision and can add 4–8 weeks (GPU provisioning, model licensing, latency re-tuning).**
2b. **India telecom onboarding (if outbound calling).** TRAI TCCCPR + DLT/header registration, operator sign-up, and consent/DND scrubbing are a **weeks-to-months** legal/business process independent of code. Start it in parallel with Phase 0 or it becomes the critical path.
3. **Provision infra** (Terraform apply): VPC, RDS Postgres+pgvector (Multi-AZ, PITR), ElastiCache Redis (TLS+AUTH), S3 + lifecycle, secrets, IAM, cluster (ECS/EKS).
4. **Stand up staging** = prod-shaped environment; run the full E2E + load + security suite there.
5. **TLS everywhere** — Postgres `sslmode=require`, Redis `rediss://`, ingress TLS + the security headers already emitted.
6. **Multi-worker rate limiting** — the in-memory limiter is single-worker; move to Redis-backed (slowapi/redis) before horizontal scale.
7. **Compliance sign-off** — DPDP consent/retention, TRAI DND + opt-out, RBI calling-hours, recording consent — reviewed and signed by legal/compliance.

---

## 3. Phased rollout plan

### Phase 0 — Deployable (2–3 wks) · *make it exist somewhere real*
- Secrets → AWS Secrets Manager; rotate exposed keys; app reads from manager, not `.env`.
- `terraform apply` in a **staging** account: RDS, ElastiCache, S3, IAM, ECS/EKS.
- Wire CI/CD to deploy to staging on merge (the GH Actions workflow exists — connect it to the registry + cluster).
- Alembic `upgrade head` as a migration Job; smoke-test `/health/ready`.
- TLS on all data stores + ingress; Redis-backed rate limiter.
- **Gate:** full stack runs in staging, health-green, one real browser call end-to-end.

### Phase 1 — Observable & safe (2–3 wks) · *know what it's doing*
- OTLP collector → Prometheus + Grafana dashboards (the metrics/SLO doc already defines them); CloudWatch/OpenSearch log sink.
- Alerting on the `docs/SLO.md` thresholds (P95>300ms, error>1%, breaker-open, cost budget).
- Sentry (frontend + backend) + Core Web Vitals.
- Cost guardrails: per-session token budget enforcement + Groq/NVIDIA spend alerts.
- Backup/restore **drill** (RDS PITR), DR runbook, SIGTERM-drain verified under real k8s rollout.
- **Gate:** an injected fault (kill NLP, spike latency) fires the right alert; a restore drill succeeds.

### Phase 2 — Quality & scale (3–4 wks) · *prove it holds up*
- **AI quality baseline:** run the RAG gold-set (P@k/recall/MRR), STT WER over a labeled Hindi/Hinglish set, intent-accuracy vs the >85% bar, a human MOS spot-check on TTS. Set regression gates.
- **Load & soak:** run `scripts/loadtest.py` at target concurrency (e.g. 50–100 sessions) + a multi-hour soak; confirm P95<300ms, no pool/Redis exhaustion, no memory creep.
- **Frontend hardening (S/T):** decompose `App.tsx` into feature modules, `strict:true`, Zustand + TanStack Query, code-split, WCAG 2.2 AA audit (axe + keyboard), Playwright E2E in CI.
- Chaos test: provider outage, DB failover, Redis eviction, pod kill mid-call.
- **Gate:** load target met, WER/intent/RAG bars met, axe clean, E2E green in CI.

### Phase 3 — Launch & operate (2 wks + ongoing) · *go live deliberately*
- Runbooks (dropped calls, provider outage, cost spike, DB failover, key rotation) + on-call rotation + incident process.
- Canary / phased rollout behind a feature flag; documented rollback drill.
- Security: external pentest, dependency/image scan gate (Trivy already in CI — flip to blocking), CSP tightened.
- Telephony (if in scope): build the Twilio/Exotel Media Streams bridge on the µ-law seam; DTMF, outbound dialer, TaskRouter escalation.
- Multi-tenancy enforcement (org scoping is coded; enable + backfill `org_id` when a 2nd tenant lands).
- **Gate:** Go/No-Go checklist (§4) fully green + sign-offs.

---

## 4. Go/No-Go launch checklist

**Security** ☐ keys rotated + in secret manager ☐ TLS on all hops ☐ pentest passed ☐ image scan blocking ☐ CSP/headers verified in prod
**Compliance** ☐ DPDP consent+retention signed ☐ TRAI DND/opt-out verified ☐ RBI calling-hours on ☐ recording-consent disclosed ☐ residency decision documented
**Infra** ☐ IaC applied ☐ Multi-AZ RDS+PITR ☐ Redis HA ☐ staging == prod ☐ blue/green or canary
**Reliability** ☐ backup/restore drilled ☐ DR runbook ☐ SIGTERM drain verified ☐ graceful degrade under provider outage
**Observability** ☐ dashboards live ☐ alerts firing on breach ☐ logs centralized ☐ cost alerts
**Quality** ☐ WER<0.10 ☐ intent>85% ☐ RAG gold-set met ☐ load P95<300ms ☐ soak clean ☐ axe AA clean
**Ops** ☐ runbooks ☐ on-call ☐ rollback drilled ☐ status page

---

## 5. What's already banked (don't re-do)

Real Groq NLP/Whisper + Orpheus/Magpie TTS with language routing & cascade fallback; resilience (circuit breaker, backoff, parallel warmup); RAG (NVIDIA embedder, HNSW, grounding citations, org-scoped retrieval); Alembic migrations + pooling; KB admin API; CRM persistence + idempotency; analytics aggregation; nullable `org_id` multi-tenancy seam + bot PATCH/audit; rate limiting + short JWT TTL + refresh + WS-token-subprotocol; PII redaction before egress; OTel metrics + traces + cost capture; health live/ready split; SIGTERM drain; hardened non-root Dockerfile; S3 recording + retention; Terraform + CI *artifacts*; ErrorBoundary; **90% test coverage**; and the 13 adversarial-audit fixes (cross-tenant IDOR, shutdown/concurrency data loss, never-drop-turn). See `docs/` (HLD, LLD, SECURITY, SLO, RESIDENCY, TELEPHONY).

## 6. Biggest risks

- **Residency / RBI localization** — highest-stakes: likely forces in-region self-hosting (weeks + GPU cost) or blocks launch. Get the legal ruling **first**; it re-shapes the whole timeline.
- **India telecom onboarding** — for a dialer, TCCCPR/DLT registration is a long lead-time gate that can dominate the schedule. Start early, in parallel.
- **Never deployed** — "works in dev Docker" ≠ "works in k8s under load with managed data stores." Concurrency (2 bugs already found), connection-pool behavior, cold starts, and **WebSocket behind a load balancer** (idle-timeout kills long calls; needs sticky routing or the shared-Redis path they have) are all prod-only unknowns.
- **Unit economics** — every turn = STT + LLM + TTS + embeddings. At scale, hosted TTS especially can make calls unprofitable. Model cost-per-call and set a target before committing to hosted providers.
- **Coverage ≠ correctness** — 90% unit coverage found 0 of the 13 audit bugs; the AI-quality evals (WER, RAG gold-set, MOS, intent) are the real quality gate and should be run *early*, not left to Phase 2.
- **Redis is a live-call SPOF** — all session/grace state is in Redis; a failover disrupts in-flight calls even with Multi-AZ. Accept, document, and test failover behavior.
- **Human handoff is a stub** — "escalate" has no live agent pool wired; product-incomplete until a contact-center/queue integration exists.
- **Frontend debt** — a 3,466-line untyped component is a maintenance + a11y liability; not a launch blocker but fast-accruing.
- **Model lock-in / drift** — pin model versions (llama-3.3-70b, whisper-large-v3-turbo, orpheus) and re-run evals on any provider upgrade; providers deprecate and re-price.
