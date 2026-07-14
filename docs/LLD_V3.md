# KOEL Voice Bot — Low-Level Design v3

> Supersedes [LLD_V2.md](./LLD_V2.md) (→ [LLD.md](./LLD.md)). Module-level view of
> the v3 codebase. Every table carries **v1 / v2 / v3** columns. Companion:
> [HLD_V3.md](./HLD_V3.md).

## What changed since v2 — module-level diff (v1 → v2 → v3)

| Area | v1 | v2 | v3 |
|---|---|---|---|
| **Pipeline modules** | `bargein, filler, silence, orchestrator` | + `endpointing, localization, transliteration, summarizer, whisper_features` | + `cost.py` (O-9 per-turn estimate + budget), speculative-RAG hold in `orchestrator` (O-5), streaming turn `_stream_bot_turn` (O-2) |
| **WS layer** | `handler, protocol` | + `recording.py` | + **`ws/streaming_stt.py`** (browser Deepgram ingest, O-6 Wave 2) + `partial_transcript` server msg |
| **STT providers** | `Stub` only | + `GroqWhisper` (pinned), `SarvamStt`, SageMaker seam | + **`DeepgramStreaming*`** (true interim partials) + Sarvam WS `stream_transcribe` (verified protocol) + per-bot `chunk`\|`ws` mode |
| **NLP** | `Stub` | + `GroqNlp` (8B/70B) | + **`nlp/streaming.py`** (`ResponseTextSentenceStreamer`) + `generate_stream()` + `/v1/respond/stream` |
| **TTS** | `Stub` | + Orpheus/Magpie/Sarvam(HTTP+WS)/Routing | + **`tts_cache.py`** (whole-clip + streaming cache, O-3) |
| **RAG embedder** | `HashEmbedder` | + NVIDIA, Bedrock seam | + **`FastEmbedEmbedder`** (`bge-m3`, O-4, opt-in) + `scripts/reembed_kb.py` |
| **Rate limiter** | in-mem `RateLimiter` | same | + **`RedisRateLimiter`** (O-8, per-route namespace, fail-open cfg) |
| **Platform (new)** | — | — | **model-connections** (encrypted secret store + SSRF guard + resolver), **Users/org registry** (6.4), connections CRUD API, EscalationHub (3.1), LiveMonitor (3.3), inbound bot (3.2) |
| **DB** | models | + KB chunks, analytics | + pool checkout metrics + `pgvector_hnsw_ef_search` + `db_pool_use_lifo` (O-10); Users/connections tables (6.4/BYOK) |
| **Frontend** | monolith `App.tsx` | + mic/VAD | **decomposed** `types/lib/components/views` + `React.lazy` + `useLiveCall` (Q-1); `useConnections`, `ConnectionsView`, `ModelConnectionSelectors`; flow editor (6.6); streaming partial captions |
| **Config surface** | ~20 | ~40 (`sarvam_*, smart_turn_*, stream_tts`, …) | + `stream_nlp, stream_stt`, Deepgram, `nlp_embedder=fastembed` + `fastembed_model`, `rate_limit_backend=redis`, `session_budget_usd`, `pgvector_hnsw_ef_search`, `db_pool_*`, `tts_cache_ttl_s`, BYOK connection config |
| **Tests** | 37 | 264 | **680** |

---

## 1. Module layout (v3 current)
Backend `koel/{common,db,stt,nlp,tts,crm,voicebot}` + `koel.combined`. New v3
surfaces: `voicebot/ws/streaming_stt.py`, `voicebot/telephony/ingest.py`
(`StreamingSessionFactory`/`StreamingSttSession` — shared by the browser Deepgram
path and future telephony bridges), `nlp/streaming.py`, `voicebot/tts_cache.py`,
`voicebot/pipeline/cost.py`, the model-connection/BYOK modules, and the Users/org
registry. Frontend `src/{types.ts,lib,components,views,hooks}` (post-Q-1).

## 2. Shared core (`koel/common`) — v1 → v2 → v3
| Module | v1 | v2 | v3 |
|---|---|---|---|
| `http.py` | — | shared async client | unchanged |
| `ratelimit.py` | in-mem | in-mem | **+ `RedisRateLimiter`**, `rate_limit(name, fail_open)` per-route namespace |
| `resilience.py` | — | breaker + retry | reused by streaming fallbacks |
| `text_guard.py` | — | PII redaction | reused per-sentence on the streaming path |
| `config.py` | ~20 | ~40 | + streaming/redis/embedder/budget/pool/cache/BYOK settings |

## 3. Data model (`koel/db/models.py`) — v3 additions
v2: `bots, sessions, call_history, knowledge_chunks` (+ pgvector). **v3 adds**
Users + org rows (6.4 multi-tenant, enforced scoping), model-connection rows
(BYOK, secret encrypted at rest), and the pgvector query knob (`hnsw.ef_search`
via `SET LOCAL`, guarded, no-op at 0).

## 4. WebSocket protocol — v1 → v2 → v3
| | v1 | v2 | v3 |
|---|---|---|---|
| client | `audio_chunk, barge_in_candidate, pong, end_call` | + `playback_done` | continuous `audio_chunk` (pcm16) frames feed the streaming session |
| server | 8 types | same | **+ `partial_transcript`** (best-effort live caption) |

## 5. Orchestrator (`voicebot/pipeline/orchestrator.py`) — v3 behavior
| Path | v2 | v3 |
|---|---|---|
| batch turn | `_respond_with_filler` → `_send_bot_turn` | unchanged when `stream_nlp` off |
| streaming turn | — | **`_stream_bot_turn`**: consume `respond_stream`, speak sentences as they land, filler race + sentiment prosody, lock-serialized; reconcile-or-escalate on a lost final |
| speculative RAG | — | **O-5**: pre-fetch on the smart-turn hold, reuse if the final hasn't diverged, cancel on teardown |
| cost/budget | — | **O-9**: `cost.py` estimate per turn + polite wrap-up at the ceiling |

## 6. WS handler `CallSession` (`voicebot/ws/handler.py`) — v3
Factory-injected `BrowserStreamingStt`: audio frames route to a long-lived
Deepgram session (partials → captions, finals → the **same** downstream turn),
degrading to the unchanged buffered per-utterance path on connect failure.
Streaming finals are **serialized by a lock** (they arrive off the receive loop,
so two close finals can't drive overlapping turns). Also wires EscalationHub
(3.1) + LiveMonitor (3.3).

## 7. AI microservices — provider inventory (v1 → v2 → v3)

### 7.1 STT (`koel/stt`)
| Class | v1 | v2 | v3 |
|---|---|---|---|
| `StubWhisperProvider` | stub | stub | stub |
| `GroqWhisperProvider` | — | real (pinned) | real; batch fallback for streaming paths |
| `SarvamSttProvider` | — | real (batch) | + `stream_transcribe`/`_stream_ws` (protocol **live-verified**: `{"audio":{"data":<b64 wav>,"encoding":"audio/wav",…}}` + `{"type":"flush"}` → `data` frame; WAV-only ⇒ single-shot, degrade to batch) |
| **Deepgram streaming** | — | — | **real, true interim partials** (`linear16`), per-utterance session, the browser streaming path |
| SageMaker | seam | seam | seam (O-7/6.8) |
| **STT mode** | n/a | `chunk` (batch) | **per-bot `chunk`\|`ws`** |

### 7.2 NLP (`koel/nlp`)
| Class/feature | v1 | v2 | v3 |
|---|---|---|---|
| `StubNlpProvider` | stub | stub | stub |
| `GroqNlpProvider` | real | + 8B/70B routing | + `generate_stream()` (token → sentence, fail-safe to batch) |
| route | `/v1/respond` | same | **+ NDJSON `/v1/respond/stream`** (always emits a terminal `final`) |
| streamer | — | — | `nlp/streaming.py::ResponseTextSentenceStreamer` (incremental JSON-value scan, never-diverge, surrogate-safe) |
| BYOK | — | — | custom LLM connection resolved per-bot |

### 7.3 TTS (`koel/tts`)
| Class/feature | v1 | v2 | v3 |
|---|---|---|---|
| Stub/Orpheus/Magpie/Sarvam/Routing | stub | all real, Sarvam WS default | unchanged |
| cache | — | — | **`tts_cache.py`**: whole-clip (`synthesize`) + streaming (drain-only) cache, keyed text·voice·lang, TTL-bounded (O-3) |
| BYOK | — | — | custom TTS connection per-bot |

### 7.4 RAG embedder (`nlp/rag.py`)
| Class | v1 | v2 | v3 |
|---|---|---|---|
| `HashEmbedder` | stub | stub | stub (fallback) |
| NVIDIA | — | real | real |
| `FastEmbedEmbedder` | — | — | **real, local `bge-m3`** (O-4, lazy-loaded, guarded, opt-in `local-embed`) |
| Bedrock | — | seam (footgun) | seam (fail-loud-at-boot fixed) |

### 7.5 CRM (`koel/crm`)
v2 `+ /call-summary`. v3: unchanged shape; summaries now surfaced in
history/dashboard (5.3).

## 8. Configuration reference (`KOEL_` env) — v3 additions
`stream_nlp` (O-2), `stream_stt` + Deepgram creds + per-bot STT mode (O-6),
`nlp_embedder=fastembed`/`fastembed_model` (O-4), `rate_limit_backend=redis` +
Redis URL (O-8), `session_budget_usd` (O-9), `pgvector_hnsw_ef_search` +
`db_pool_use_lifo`/`db_pool_*` (O-10), `tts_cache_ttl_s` (O-3), BYOK
model-connection config. All default-off/inert so v2 behavior is preserved.

## 9. REST API — v3 additions
+ `POST /v1/respond/stream` (NLP NDJSON); + model-connections CRUD (BYOK); +
Users/org endpoints (6.4); + escalation/monitor endpoints (3.1/3.3). `/refresh`
now consumed by the FE (Q-2).

## 10. Key algorithms — v3 additions
- **Incremental JSON sentence extraction** (`nlp/streaming.py`) — emits reply
  sentences as tokens arrive; provably a prefix of the final parse.
- **Speculative RAG reuse** (O-5) — fire on hold, reuse iff the final ≈ the
  speculated partial, else discard.
- **Streaming-final serialization** (`ws/streaming_stt.py`) — `asyncio.Lock`
  so callback-driven finals never overlap the turn loop.
- **`SET LOCAL hnsw.ef_search`** (O-10) — transaction-scoped recall/latency knob.
- **BYOK secret handling** — encrypt-at-rest + SSRF-guarded egress + resolver.

## 11. Testing strategy (`backend/tests`, 680 tests) — v1 → v2 → v3
37 → 264 → **680**. New v3 suites: NLP streaming (foundation + provider + route),
VB `respond_stream` client, O-2 orchestrator turn (incl. adversarial-review
hardening: lost-final reconcile, empty-stream, cancel-safe, prosody), O-3 cache,
O-4 embedder, O-8 Redis limiter (namespace + fail-closed), O-9 cost/budget, O-10
pool + `ef_search`, O-6 streaming STT + **concurrent-finals serialization**,
BYOK/multi-tenant/escalation. Frontend e2e (Q-7): call-flow, navigation,
edge-sims (barge-in/escalation/network-drop), streaming-partials — cross-browser
(chromium/firefox/webkit; a fake-mic assertion skips on webkit).

## 12. Extension seams — v3
`telephony/ingest.py` `StreamingSessionFactory` is the shared seam for the
browser Deepgram path **and** future Twilio/Exotel bridges (Theme 1). BYOK
resolver is the seam for any new provider. O-7 (in-region/SageMaker) remains the
open residency seam.
