# KOEL — Product Requirements Document v3 (Conformance Status)

> Supersedes [PRD_V2.md](./PRD_V2.md). Tracks product conformance across the three
> build eras. v3 = after the latency/cost pass (O-1…O-10), the quality pass
> (Q-1…Q-7), and the first Part-C features. Companions: [HLD_V3.md](./HLD_V3.md),
> [LLD_V3.md](./LLD_V3.md).

## Conformance at a glance (v1 → v2 → v3)

| PRD area | v1 | v2 | v3 |
|---|---|---|---|
| Multilingual EN/HI/Hinglish | stub voices | real (Orpheus/Magpie/Sarvam), script-correct | unchanged + per-bot BYOK voice |
| STT | type-to-talk stub | Groq Whisper (pinned) + Sarvam | **+ Deepgram streaming (interim partials) + per-bot chunk\|ws** |
| NLP intent/response/tone | real Groq | language-routed 8B/70B | **+ first-token streaming**; sentiment-adaptive tone (4.3) |
| RAG grounding | hash stub | NVIDIA embedder + 3 seeded KBs + upload | **+ local FastEmbed + speculative retrieval** |
| Perceived latency | thesis | filler + sentence-streaming TTS | **token-bound first-audio** (O-1/O-2/O-6) |
| Barge-in / endpointing | design | dual-gate + smart-turn v3.2, tested | preserved across the streaming path |
| Compliance (TRAI/DPDP) | design | enforced in the turn loop | preserved on streaming; bounded TTS-cache PII; enforced org isolation |
| CRM read/flag/summary | endpoints | + post-call summary | summaries surfaced in history/dashboard (5.3) |
| Dashboard | full SPA (type-to-talk) | mic + VAD | **decomposed + code-split; live KPIs (Q-3); connections manager; flow editor (6.6)** |
| Auth | JWT | +refresh (FE gap) | **persisted + silent refresh (Q-2)**; real Users + org scoping (6.4) |
| Human escalation | design | design | **live agent handoff (3.1) + supervisor monitor (3.3)** |
| Inbound | out of scope | out of scope | **inbound bot (3.2)** |
| Cost governance | none | none | **per-call economics + budget caps (O-9)** |
| Extensibility | env-fixed providers | env-fixed | **BYOK per-bot LLM/TTS/STT connections** |

## New product surfaces in v3
- **Model connections (BYOK):** tenants bring their own LLM/TTS/STT keys/endpoints;
  secrets encrypted at rest, egress SSRF-guarded, resolved per bot.
- **Multi-tenant:** real Users table, per-login org scoping enforced end to end.
- **Human-in-the-loop:** escalation hub + live supervisor monitor; inbound bot.
- **Conversation-flow editor:** visual + form editor for scripts.

## Non-conformance / deferred (see [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md))
In-region inference (O-7, legal-gated); PSTN bridges (Twilio/Exotel — the ingest
seam exists); payments/WhatsApp/scheduled-callbacks (Theme 2); advanced analytics
(6.2). Requirements unchanged from v2 except where the table above marks progress.
