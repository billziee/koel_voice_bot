# KOEL — Production Readiness Masterplan v4

> Supersedes [PRODUCTION_MASTERPLAN_V3.md](./PRODUCTION_MASTERPLAN_V3.md). Marks the readiness domains advanced in v4 — dominated by one change: **telephony reached full feature parity with the browser transport** (PR #65), moving Theme 1 out of "remaining" entirely.

## Readiness by domain (v1 → v2 → v3 → v4)

| Domain | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| AI pipeline real | NLP only | STT/NLP/TTS/RAG all real | + streaming (NLP/STT), cache (TTS), local embedder | unchanged |
| Multi-worker correctness | single | single | Redis rate limiter (O-8) + pool observability (O-10) | unchanged |
| Cost/unit economics | gap | gap | per-call estimate + budget caps (O-9) | unchanged |
| Multi-tenancy | seam | seam | enforced (real Users + org scoping, 6.4) | + org/user admin provisioning (invite/accept/manage, `api/users.py` + `api/orgs.py`) with FE (`AdminView`, accept-invite link) |
| Provider flexibility | env-fixed | env-fixed | BYOK connections (encrypted + SSRF-guarded) | + AWS providers now REAL code (`WhisperSageMakerProvider`, `OrpheusSageMakerProvider`, `BedrockTitanEmbedder` — boto3, not stubs); no endpoint/model-access grant deployed yet |
| Human-in-the-loop | design | design | live handoff (3.1) + monitor (3.3) + inbound (3.2) | + PSTN warm transfer (Twilio Conference API, 3.1 Phase B) + escalation now actually **gates** telephony audio on hold (previously the bot kept talking over a customer mid-handoff — fixed) |
| Telephony / PSTN (Theme 1) | gap | µ-law seam | seam (browser-only demo) | **FULLY BUILT** — `TelephonyCallSession` (`telephony/session.py`) brings Twilio calls to full parity with the browser: Gate 1 (RMS-VA) + Gate 2 barge-in, no-input watchdog, smart-turn hold + speculative RAG (`pipeline/specrag.py`), escalation registration+gating, REST hangup (`pipeline/warm_transfer.end_twilio_call`), idle-frame watchdog, CallSid persisted to Redis on `start` |
| Call recording | bot-only, browser-only | bot-only, browser-only | bot-only, browser-only | **bidirectional** (bot + customer audio) on **both** transports — previously only the bot's half was ever recorded; telephony calls previously weren't persisted to Postgres **at all** — now they are |
| Outbound calling | none | none | none | outbound dialer built (`pipeline/outbound_dialer.py`, `pipeline/dialer_worker.py`, `campaigns.py` — origination, `DialAttempt`/`Campaign` tables) |
| Frontend maintainability | monolith | monolith + mic | decomposed + code-split + strict TS (Q-1/Q-4) | unchanged |
| CI gate | chromium advisory | chromium | cross-browser blocking (Q-5) | unchanged — **GitHub Actions itself is currently blocked** (billing/spend-limit, not a code/test failure); verify locally (`ruff`, `mypy`, `pytest`) until resolved |
| Observability collector | none | none | still Phase-1 (metrics emitted, no Grafana here) | unchanged |
| In-region inference | seam | seam | seam (O-7, legal-gated) | seam, unchanged — legal ruling still outstanding; embedder side of the seam is now real code (Bedrock Titan v2), not a `NotImplementedError` stub, but still needs a Bedrock model-access grant in `ap-south-1` |
| Security hardening | — | — | — | 4 bug-hunt review batches landed: cross-tenant IDOR fix, WS finalize/data-loss cluster, medium/low correctness+authz fixes, telephony frame-parse hardening (Twilio signature verification bug + stateful-codec-resampling bug fixed) |
| Tests | 37 | 264 | 680 | 913 (1 skipped, 0 failing) |
| Coverage | — | — | — | 91.87% measured, against an 85% CI gate (`pyproject.toml fail_under=85`) — new row, not tracked numerically pre-v4 |

## Remaining for GA
In-region inference / RBI-DPDP-IRDAI residency legal ruling (O-7 — still the single biggest blocker; Sarvam now covers Hindi/Hinglish STT+TTS in-region as a partial mitigation, NLP+English STT/TTS still egress to US Groq/NVIDIA); observability collector + dashboards; Theme-2 revenue actions (payments/WhatsApp/callbacks); 6.2 analytics; `terraform apply` + real AWS deployment (infra/terraform/ is written and validated but never applied — no VPC/RDS/ElastiCache/ECS actually exists; SageMaker endpoints deliberately not provisioned, since faking one with placeholder values would be worse than the honest gap); staging environment (still none — Render is the only live deployment); credential rotation (Groq, NVIDIA, Sarvam, Deepgram keys, plus the Render API token — all pasted in plaintext during dev, none rotated yet).

~~PSTN bridges (Theme 1)~~ — done; moved into the readiness table above.
