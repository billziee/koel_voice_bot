# KOEL — Production Readiness Masterplan v3

> Supersedes [PRODUCTION_MASTERPLAN.md](./PRODUCTION_MASTERPLAN.md). Marks the
> readiness domains advanced in v3.

## Readiness by domain (v1 → v2 → v3)

| Domain | v1 | v2 | v3 |
|---|---|---|---|
| AI pipeline real | NLP only | STT/NLP/TTS/RAG all real | + streaming (NLP/STT), cache (TTS), local embedder |
| Multi-worker correctness | single | single | **Redis rate limiter (O-8) + pool observability (O-10)** |
| Cost/unit economics | gap | gap | **per-call estimate + budget caps (O-9)** |
| Multi-tenancy | seam | seam | **enforced (real Users + org scoping, 6.4)** |
| Provider flexibility | env-fixed | env-fixed | **BYOK connections (encrypted + SSRF-guarded)** |
| Human-in-the-loop | design | design | **live handoff (3.1) + monitor (3.3) + inbound (3.2)** |
| Frontend maintainability | monolith | monolith + mic | **decomposed + code-split + strict TS (Q-1/Q-4)** |
| CI gate | chromium advisory | chromium | **cross-browser blocking (Q-5)** |
| Observability collector | none | none | still Phase-1 (metrics emitted, no Grafana here) |
| In-region inference | seam | seam | seam (O-7, legal-gated) |
| Tests | 37 | 264 | **680** |

## Remaining for GA
Observability collector + dashboards; O-7 residency ruling + in-region providers;
PSTN bridges (Theme 1); Theme-2 revenue actions (payments/WhatsApp/callbacks);
6.2 analytics. See [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md) for the ordered
backlog and [SLO_V3.md](./SLO_V3.md)/[SECURITY_V3.md](./SECURITY_V3.md) for the
operational gates.
