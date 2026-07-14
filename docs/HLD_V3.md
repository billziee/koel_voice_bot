# KOEL Voice Bot — High-Level Design v3

> Supersedes [HLD_V2.md](./HLD_V2.md) (which superseded [HLD.md](./HLD.md)). v3
> is the current state after the latency/cost optimization pass (O-1…O-10), the
> code-quality/engineering pass (Q-1…Q-7), and the first wave of Part-C product
> features (telephony seams, human-in-the-loop, multi-tenant, BYOK model
> connections). Every table below carries a **v1 / v2 / v3** column so the
> evolution is legible end-to-end.

## What changed since v2 — at a glance

v2 made the AI pipeline fully real and deployed it live. v3 attacks the two
things v2 flagged as open: **true latency** (the streaming + speculation +
caching arc) and **product/platform depth** (multi-tenant, BYOK, telephony,
human handoff). It also hardened the code surface: the 3.6k-line `App.tsx`
monolith is decomposed + code-split, TypeScript is on full `strict`, the rate
limiter is Redis-backed for multi-worker correctness, and the test count went
264 → **680**.

| Area | v1 (HLD.md) | v2 (HLD_V2.md) | v3 (now) |
|---|---|---|---|
| **STT** | Stub (type-to-talk) | Groq Whisper (pinned) + Sarvam `saaras` (batch) | **+ Deepgram streaming** (true interim partials) + **per-bot STT mode** `chunk`\|`ws` + browser continuous-frame streaming ingest; Sarvam WS protocol verified/corrected (WAV single-shot); **+ BYOK custom STT** |
| **NLP** | Real Groq (only real piece) | Language-routed 8B/70B, JSON-mode, language-strict prompt | **+ O-2 first-token streaming** (`/v1/respond/stream`, sentence-by-sentence into TTS) behind `stream_nlp`; **+ BYOK custom LLM** connection |
| **TTS** | Stub (sized PCM) | 3 engines (Orpheus/Magpie/Sarvam), Sarvam WS default, routed | **+ O-3 response cache** (whole-clip + streaming, keyed text·voice·lang, TTL-bounded for PII); **+ BYOK custom TTS** |
| **RAG embeddings** | `HashEmbedder` stub | NVIDIA `nv-embedqa-e5-v5` (1024-d) | **+ O-4 local FastEmbed** `bge-m3` (opt-in, no US hop); **+ O-5 speculative retrieval** on the endpointing hold |
| **Turn latency** | "sub-300ms is the product" (thesis) | Renegotiated + measured; filler + sentence-streaming TTS | **Token-bound path**: O-1 warm-up + pre-synth greeting, O-2 NLP streaming, O-3 cache, O-4 local embedder, O-5 speculative RAG, O-6 streaming STT partials → first-audio no longer whole-turn-bound |
| **Auth / tenancy** | JWT REST+WS | +`/refresh`; FE didn't persist (Q-2 gap) | **Q-2 JWT persistence + silent refresh**; **6.4 real Users table + enforced per-login org scoping** (multi-tenant registry) |
| **Rate limiting** | In-memory, single-worker | Same | **+ O-8 Redis backend** (correct at >1 worker), per-route namespace, fail-closed login |
| **Cost controls** | None | None | **+ O-9 per-call unit economics + per-session budget caps** |
| **DB / retrieval** | Default pool | Default pool | **+ O-10 pool checkout observability + pgvector `ef_search` knob + LIFO + load-test harness** |
| **Frontend** | Type-to-talk SPA | Real mic + Silero VAD + barge-in; monolithic `App.tsx` | **Q-1 decomposed + React.lazy code-split + `useLiveCall` hook**; continuous-frame streaming + live partial captions; **connections manager + per-bot model selectors + conversation-flow editor** |
| **Human-in-the-loop** | — | — | **3.1 live agent handoff** (EscalationHub), **3.2 inbound bot**, **3.3 live supervisor monitor** |
| **Telephony** | Browser WS only | Browser WS only | **Streaming-session ingest seam** (`telephony/ingest.py`) shared by the browser Deepgram path; Twilio/Exotel bridges are the next build on it |
| **Model providers** | Fixed (env) | Fixed (env) | **BYOK model connections**: encrypted secrets (at rest) + SSRF guard + resolver; per-bot custom LLM/TTS/STT selection |
| **TypeScript** | loose | loose | **Q-4 full `strict`** (CI-gated) |
| **CI** | chromium-only, advisory | chromium-only | **Q-5 cross-browser gate** (chromium/firefox/webkit), blocks merge |
| **Tests** | 37 | 264 | **680** |

See [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md) for what remains (O-7
in-region inference, remaining Part-C themes), and [9 june decisions.md](./9%20june%20decisions.md)
+ [O5-speculative-rag-design.md](./O5-speculative-rag-design.md) for the v3 build log.

---

## 1. Purpose & scope

Unchanged from v1/v2: a multilingual (EN / HI / Hinglish) outbound voice-bot for
Indian financial services — 5 FastAPI microservices (STT, NLP, TTS, CRM,
VoiceBot orchestrator) behind a React SPA, run combined as `koel.combined:app`
on the free tier. v3 adds a genuine multi-tenant/BYOK platform layer on top.

## 2. Goals & non-functional requirements

| Dimension | v1 target | v2 measured/renegotiated | v3 direction |
|---|---|---|---|
| First-audio p50 | <300ms (turn) | ~1.0s (perceived <300ms via filler) | Token-bound; O-2+O-6 shrink the true floor toward ~0.5–0.7s (US inference) |
| Concurrency | design-only | 0% err @ 10 concurrent (measured) | O-8 (Redis limiter) + O-10 (pool) make >1 worker correct + observable |
| Cost visibility | none | none | O-9 per-call estimate + hard per-session ceiling |
| Multi-tenant | seam | seam | Enforced org scoping at login + per-row (6.4) |
| Never-drop-a-turn | design | tested (264) | Preserved through every streaming/speculative path (680 tests) |

## 3. System context

The five services and the SPA are unchanged in shape. New **external
dependencies** in v3: **Deepgram** (streaming STT WebSocket), **Redis** (rate
limiter / warm-connection pool at >1 worker), optional **FastEmbed/ONNX** (local
embeddings), and **per-tenant BYOK provider endpoints** (validated through an
SSRF guard before use).

## 4. Component responsibilities (v3 deltas)

| Component | v2 | v3 additions |
|---|---|---|
| VoiceBot orchestrator | turn loop, resilience, filler/barge-in | O-2 streaming turn (`_stream_bot_turn`), O-5 speculative-RAG hold, O-9 cost/budget guard, EscalationHub + LiveMonitor wiring |
| WS handler | buffered per-utterance STT | + browser streaming ingest (`ws/streaming_stt.py`, factory-injected, degrade-to-buffered), serialized streaming finals |
| STT svc | Groq/Sarvam batch | + Deepgram streaming provider, per-bot `chunk`\|`ws` mode |
| NLP svc | batch `/v1/respond` | + NDJSON `/v1/respond/stream` |
| Platform (new) | — | Model-connection registry (BYOK), Users/org registry, connections CRUD API |

## 5. Technology stack (v1 → v2 → v3)

| Layer | v1 | v2 | v3 |
|---|---|---|---|
| STT | stub | Groq Whisper, Sarvam | + Deepgram (streaming) |
| NLP | Groq | Groq 8B/70B | + streaming; BYOK LLM |
| TTS | stub | Orpheus/Magpie/Sarvam(WS) | + response cache; BYOK TTS |
| Embeddings | hash | NVIDIA | + FastEmbed local |
| Rate limit | in-mem | in-mem | + Redis |
| Secrets | env | env | + encrypted BYOK store + SSRF guard |
| FE | monolith SPA | monolith + mic/VAD | decomposed + code-split + streaming captions |

## 6. Key data flows

### 6.1 Outbound turn — v3 streaming happy path
Mic frames → (per-bot mode) **Deepgram streaming session** → interim
`partial_transcript` captions + an utterance `final` → the **same**
`handle_final_transcript` the buffered path uses (control-flow, barge-in,
cost/budget unchanged) → **O-2**: NLP streams reply sentences → each fed to
sentence-streaming TTS as it lands (**O-3** cache short-circuits repeats) →
audio streamed back. **O-5** speculative RAG may have pre-fetched on the
endpointing hold. Falls back to the v2 buffered/batch path on any streaming
failure — a turn is never dropped.

### 6.2 Resilience/degradation (v3)
Every new fast path degrades: streaming STT → buffered transcribe; NLP stream →
batch `respond()`; TTS cache miss → synth; Redis limiter → fail-open (except
login, fail-closed); local embedder load failure → hash; BYOK resolve failure →
env default.

## 7. Cross-cutting concerns

| Concern | v1 | v2 | v3 |
|---|---|---|---|
| PII egress | redact before TTS/logs | same | + TTS-cache TTL bounds derived-audio PII retention |
| Secrets | env only | env only | BYOK secrets encrypted at rest; SSRF guard on custom endpoints |
| Concurrency | single worker | single worker | Redis limiter + serialized streaming finals + pool observability |
| Tenancy | soft | soft (org_id seam) | enforced org scoping at login + per-row |

## 8. Deployment view
Unchanged base (Render, `koel.combined:app` from `main`, self-migrating Alembic,
keep-alive). v3: **Q-6** reconciled `render.yaml` ↔ `Dockerfile` (env-prefix + fetch-script
fixes); optional Redis + Deepgram + BYOK config are additive and default-off.

## 9. Compliance & security posture

| Item | v1 | v2 | v3 |
|---|---|---|---|
| DPDP data-minimisation | design | redaction | + bounded TTS-cache retention; per-tenant isolation |
| TRAI opt-out / DND | design | enforced in turn loop | preserved across streaming path (reconcile-or-escalate on lost final) |
| Audit trail | design | per-turn | partial-turn recorded even on cancellation |
| SSRF | n/a | n/a | guard on all BYOK provider URLs |

## 10. Scalability & resilience
v3 makes >1 worker correct (Redis limiter O-8), observable (pool metrics O-10),
and cost-bounded (O-9). Streaming paths add independent failure domains, all
degrading to the proven buffered path.

## 11. Current state — real vs. stub (v1 → v2 → v3)

| Capability | v1 | v2 | v3 |
|---|---|---|---|
| NLP | real Groq | + language-routed | **+ streaming + BYOK LLM** |
| RAG | pgvector + hash stub | + NVIDIA embedder | **+ local FastEmbed + speculative** |
| STT | stub | Groq+Sarvam batch | **+ Deepgram streaming + per-bot mode** |
| TTS | stub | 3 engines + WS | **+ cache + BYOK TTS** |
| Endpointing | fixed timer | + smart-turn v3.2 | drives O-5 speculation |
| Barge-in | design | tested live | unchanged across streaming path |
| Rate limit | in-mem | in-mem | **Redis (real, multi-worker)** |
| Auth/tenancy | JWT | +refresh (FE gap) | **persisted + refresh + enforced org scoping** |
| Human handoff | — | — | **real (EscalationHub + monitor)** |
| Frontend | type-to-talk | mic+VAD monolith | **decomposed + code-split + streaming captions** |

## 12. Risks & future work
See [FUTURE_PROCEEDINGS.md](./FUTURE_PROCEEDINGS.md). Headline remaining: **O-7**
in-region inference (legal-gated), O-6 handler-ingest hardening at telephony
scale, remaining Part-C themes (Twilio/Exotel bridges, payments, WhatsApp,
analytics). Module-level detail: [LLD_V3.md](./LLD_V3.md).
