# KOEL — Future Proceedings: Optimizations, Advancements & Features

> **Scope.** This document covers product **features** and **code advancements** —
> what to build next, and how. It is not the deployment/operations plan: infra
> provisioning, secrets, staging, DR, observability wiring, compliance sign-off,
> and telecom onboarding phasing all live in
> [`PRODUCTION_MASTERPLAN.md`](./PRODUCTION_MASTERPLAN.md) — items below cite it
> as a prerequisite where relevant rather than restating it. Telephony
> architecture detail lives in [`TELEPHONY.md`](./TELEPHONY.md); AWS-residency
> provider work in [`RESIDENCY.md`](./RESIDENCY.md); measured latency numbers in
> [`SLO.md`](./SLO.md).
>
> **Effort scale:** **S** < 1 week · **M** 1–3 weeks · **L** 3+ weeks (single
> engineer). Items are ordered by ROI within each part.
>
> **Ordering — latency & cost first.** This doc is deliberately structured
> **Part A (optimizations) → Part B (quality/enablers) → Part C (features)**,
> per an explicit product decision to prioritize turn-latency and unit economics
> before platform breadth. (The source design passes for this document produced
> features first, optimizations second — the content is unchanged, only the
> reading order is flipped, and the two are cross-referenced throughout.)
>
> **Measured baseline** (2026-07, live load test vs the deployed Render US-East
> backend, from India): first-audio **p50 ≈ 0.9–1.0s**, **p95 ≈ 2.1–2.3s**,
> **p99 ≈ 6s**, **0% errors at 10 concurrent sessions**. Latency is
> **inference + ocean-RTT bound, not load-bound** — a single session performs
> about the same as 10 concurrent ones. Cold first turns of a session sometimes
> time out. SLO: *perceived* first-audio **&lt;300ms** (met today via filler audio
> + sentence-streaming TTS); *true* first-audio target **p50 &lt;600ms** after
> Part A items O-1…O-4 land, and **~0.4–0.6s from India** after O-7 (in-region
> inference) — see [`SLO.md`](./SLO.md) for the full breakdown.

---

## Item zero — fix the footgun — DONE

Both halves shipped: `build_embedder()` fails loudly at boot (not mid-call) if
config selects a still-unimplemented embedder (`_UNIMPLEMENTED_EMBEDDERS`
guard), and separately, `BedrockTitanEmbedder` itself is no longer a stub —
see item **6.8** in Part C for the full status of the SageMaker/Bedrock track.

---

# Part A — P0 · Latency & Cost Optimizations

## Priority Index (ROI-ordered)

| # | Item | Effort | Expected Gain |
|---|------|--------|---------------|
| O-1 | Session warm-up + pre-synthesized greeting | S | Kills cold-turn timeouts; p99 6s → ~2.5s |
| O-2 | NLP first-token streaming (Tier 3) | M-L | −200–500ms perceived first-audio (biggest on hi/70b) |
| O-3 | TTS response cache keyed (text, voice, lang) | S | −300–800ms on cache hits (est. 10–30% of turns) |
| O-4 | Local embedder (Tier 4) | S-M | −200–250ms per RAG turn; kills a US round-trip |
| O-5 | Speculative RAG ∥ STT finalization | M | −50–250ms (shrinks after O-4) |
| O-6 | Streaming STT partials | M-L | −300–500ms end-of-utterance → transcript |
| O-7 | In-region inference (the big one) | L | −250ms+ RTT per provider call; residency compliance |
| O-8 | Redis-backed rate limiter | S | Correctness at >1 worker (prereq for scaling) |
| O-9 | Per-call unit economics + budget caps | M | Cost visibility; prevents runaway spend |
| O-10 | DB pool & retrieval tuning | S | Headroom at 50+ concurrent |

## Optimizations

### O-1 · Session warm-up + pre-synthesized greeting — S

**What:** Eliminate cold first-turn timeouts by pre-warming provider connections at session start and synthesizing the greeting during the WS handshake instead of after it.

**Why:** Measured p99 ~6s and occasional first-turn timeouts are almost entirely cold-path: first request on a fresh HTTP/2 connection pays TCP+TLS+HTTP/2 setup to Groq/Sarvam/NVIDIA across the ocean, stacked on provider-side cold start.

**Files:** `backend/koel/voicebot/ws/handler.py`, `backend/koel/voicebot/pipeline/orchestrator.py` (`greet()`, line 220), `backend/koel/voicebot/clients.py`, `backend/koel/common/http.py`.

**Approach:** At WS accept, fire a background `asyncio.gather` of no-op/tiny warm-up calls through `ServiceClients` (e.g., `HEAD`/health per service, or a 1-token NLP ping) so the `make_async_client` keep-alive pools (300s expiry — already long enough to survive the session) are hot before the caller speaks. In parallel, start `greet()`'s TTS synthesis during the handshake — the greeting text is fully computable from `state.greeting_template` + CRM context before any audio arrives — and hold the result until the client signals ready. The 300s `keepalive_expiry` in `common/http.py` already prevents mid-session re-cold-starts; this closes the session-start hole.

**Gain:** Cold-turn timeouts → ~0; p99 from ~6s toward the warm p95 (~2.5s); greeting starts ~0ms after client-ready.

**Dependencies:** None. Do first.

### O-2 · NLP first-token streaming (Tier 3) — M-L

**What:** Stream Groq completion tokens, extract `response_text` sentences incrementally from the JSON-mode output, and pipe them into the existing sentence-streaming TTS — instead of waiting for the full completion.

**Why:** The deepest remaining perceived-latency seam. Today `nlp/main.py` exposes only `POST /v1/respond` and `GroqNlpProvider.generate()` blocks until the full 160-token JSON body lands; sentence-streaming TTS (Tier 2) then starts. Full completion ≈ TTFT + generation (~200ms for 8B-en, ~500–700ms for 70B-hi); the first sentence is ready after ~25–40 of those tokens.

**Files:** `backend/koel/nlp/providers.py` (`GroqNlpProvider`), `backend/koel/nlp/main.py` (new route), `backend/koel/voicebot/clients.py` (`respond()` sibling `respond_stream()`), `backend/koel/voicebot/pipeline/orchestrator.py` (`_send_bot_turn`, sentence loop at ~line 182).

**Approach:** Add `generate_stream()` to `GroqNlpProvider` using `stream=True`; run an incremental JSON scanner over the token stream that yields text the moment it's inside the `"response_text"` string value, split on sentence boundaries with the existing `split_sentences()` logic (`tts/providers.py:95` — lift to a shared module). Expose as NDJSON `POST /v1/respond/stream` (first frames: sentences; final frame: the full parsed `NlpResponse` for intent/escalation/state fields). In the orchestrator, feed each sentence into `stream_synthesis` as it arrives rather than after `respond()` returns; keep the batch route as the fallback for stub/failure paths. Stacks multiplicatively with Sarvam WS TTS (commit `f3709b4`) — first sentence → first audio is then token-bound, not turn-bound.

**Gain:** −200–500ms true first-audio p50 (largest for hi/hinglish→70B); p50 toward ~0.5–0.7s.

**Dependencies:** None hard; O-1 recommended first so streaming lands on warm connections. JSON-mode + streaming interplay needs a guardrail: fall back to batch parse if the scanner desyncs.

### O-3 · TTS response cache keyed (text, voice, lang) — S

**What:** LRU-cache synthesized audio for repeated bot lines beyond the existing filler set.

**Why:** Objection-handling scripts repeat: disclosures, consent asks, reprompts, common objection responses. Greeting/reprompt/close are already covered by `FillerCache` (`voicebot/pipeline/filler.py`), but every other repeated line pays full synthesis (300–800ms) plus ocean RTT.

**Files:** `backend/koel/voicebot/clients.py` (`synthesize`/`stream_synthesis`), `backend/koel/voicebot/pipeline/filler.py` (pattern to generalize), `backend/koel/tts/main.py` (alternative: cache server-side).

**Approach:** Wrap `ServiceClients.synthesize`/`stream_synthesis` with an in-process LRU (`(sha256(text), voice, language, prosody)` → PCM, ~50–100MB budget, TTL a few hours) following the `FillerCache` shape. Cache only completed whole-clip results; streaming path checks the cache first and replays chunks locally on hit. For multi-worker deployments later, promote to Redis with the same key (Redis already provisioned in `render.yaml`).

**Gain:** ~0ms synthesis on hits; estimated 10–30% of turns in scripted collections flows; also cuts TTS spend proportionally.

**Dependencies:** None. Note interplay with O-2: cache keys on full sentence text, so hit-rate is best on templated lines, unaffected by streaming.

### O-4 · Local embedder (Tier 4) — S-M

**What:** Add a fastembed/ONNX CPU embedder branch so RAG retrieval stops paying a US round-trip to NVIDIA per turn.

**Why:** `nlp/main.py:respond()` calls `retrieve()` inline before generation; `NvidiaEmbedder` (`nlp/rag.py:53`) is ~250ms+ from India-adjacent regions vs ~10–40ms local CPU. This is on the critical path of every RAG turn.

**Files:** `backend/koel/nlp/rag.py` (`Embedder` ABC line 26, `build_embedder()` line 103), `backend/koel/nlp/config.py`, `backend/koel/db/models.py` (`EMBEDDING_DIM = 1024`), KB ingestion path (`nlp/chunking.py` + wherever chunks are written).

**Approach:** Implement `FastEmbedEmbedder(Embedder)` using a 1024-d multilingual model (BAAI/bge-m3 via fastembed is 1024-d and handles Hindi/Hinglish — matches `EMBEDDING_DIM=1024` so no schema migration) and add an `embedder_provider` setting branch in `build_embedder()`, mirroring the existing stub/NVIDIA/Bedrock switch. Ship the ONNX model via a fetch script following `scripts/fetch_smart_turn_model.sh` (the same pattern now used to ship the smart-turn model — see the WS-streaming changelog in `docs/HLD_V2.md`). **Query and document vectors must come from the same model** — re-embed all KB chunks on switch (one-off migration script; HNSW index rebuilds automatically on upsert).

**Gain:** −200–250ms per RAG turn; removes one of the residency-problematic US calls (partial credit toward O-7).

**Dependencies:** KB re-embed migration. Do before O-5 (changes its cost/benefit math).

### O-5 · Speculative / parallel pipeline — M

**What:** Run RAG retrieval concurrently with STT finalization; optionally pre-fetch likely next chunks.

**Why:** Today the turn is strictly serial: STT completes → `/v1/respond` embeds + retrieves → LLM. Smart-turn v3.2 endpointing (`pipeline/endpointing.py`) gives a semantic "turn is ending" signal *before* the final transcript, and interim transcript text is usually good enough for retrieval.

**Files:** `backend/koel/voicebot/pipeline/orchestrator.py` (`handle_final_transcript`), `backend/koel/nlp/main.py` (factor `retrieve()` block into a callable/route), `backend/koel/voicebot/clients.py`.

**Approach:** Expose retrieval as `POST /v1/retrieve` (thin wrapper over the existing `retrieve()` call with the same per-bot overrides), and have the orchestrator kick it off with the best-available transcript the moment endpointing crosses the "likely done" threshold, `await`ing the result inside `/respond` handling (pass pre-retrieved chunks in `TurnContext` so the NLP service skips its own retrieval — the field addition is additive to `nlp/schemas.py`). Guard: if the final transcript diverges materially from the speculative one (normalized token overlap below threshold), re-retrieve.

**Gain:** Hides most of retrieval latency: −200–300ms with NVIDIA embedder, −30–80ms after O-4. Sequence after O-4 and only if profiling still shows retrieval on the critical path.

**Dependencies:** O-4 (re-scopes the win); endpointing signal already merged (`e1d6c38`).

### O-6 · Streaming STT partials — M-L (browser: done; telephony barge-in extension: done)

**Status (updated):** The browser-transport half described below shipped (Sarvam WS streaming STT, `BrowserStreamingStt` in `backend/koel/voicebot/ws/streaming_stt.py`, enabled by default). Separately, telephony (PSTN) had interim STT partials arriving (`StreamingTelephonyIngest`/`DeepgramStreamingSession`) but nothing acted on them for barge-in — flagged in an earlier audit as "no partial-transcript barge-in" for the carrier transport specifically. Now closed: `backend/koel/voicebot/telephony/bargein_gate.py`'s `TelephonyBargeInGate` + `TwilioMediaBridge.is_bot_speaking` treat the interim partial's mere non-empty arrival (while the bot is speaking) as the carrier-side Gate 1 stand-in (no client VAD worklet exists on a phone call), running the same `BargeInClassifier` Gate 2 the browser path already had.

**What (original, browser path):** Real-time STT so the transcript is (mostly) ready when the user stops talking.

**Why:** Both providers are batch REST (`stt/providers.py`: `GroqWhisperProvider.transcribe`, `SarvamSttProvider.transcribe`) — the full utterance uploads *after* end-of-speech, costing ~300–800ms before NLP can start. Groq Whisper is batch-only; Sarvam ships a WebSocket streaming STT (`speech-to-text/streaming` on `api.sarvam.ai` — **verify current API shape at implementation time**).

**Files:** `backend/koel/stt/providers.py`, `backend/koel/stt/main.py`, `backend/koel/voicebot/ws/handler.py`, `backend/koel/voicebot/clients.py`, browser capture path `src/lib/vad.ts` + audio sender.

**Approach:** Mirror the Sarvam WS TTS pattern (`SarvamTtsProvider._stream_ws`, `tts/providers.py:454`) in reverse: add a `stream_transcribe()` seam on `SttProvider`, implemented first for Sarvam WS, with batch Groq Whisper as the unchanged fallback (the routing/fallback shape already exists from commit `c2e41d5`). The voicebot WS handler forwards audio frames to the STT stream as they arrive instead of buffering to one POST; partials feed O-5's speculative retrieval, and the final partial + smart-turn endpoint decision replaces the batch call. Combined with O-2, the serial chain STT→NLP→TTS becomes three overlapping streams.

**Gain:** −300–500ms end-of-utterance → transcript; with O-2 this is the path to true first-audio p50 ≲500ms without leaving US inference.

**Dependencies:** Sarvam WS STT availability/verification; O-5 to exploit partials fully. Highest integration risk of the latency items — do behind a flag like the Sarvam TTS rollout (`91e2cc8` → `f3709b4`; **now enabled by default**, see the WS-streaming changelog).

### O-7 · In-region inference — L (the big one)

**What:** Move inference (STT/NLP/TTS/embeddings) to India: either expand Sarvam (already `api.sarvam.ai`, India-hosted — **confirm hosting region + DPA in writing**) to cover NLP via its chat-completions API (post-call summarizer already uses it, commit `6af0c55`), or self-host in `ap-south-1` via the existing SageMaker/Bedrock stubs.

**Why:** The ~250ms ocean RTT per provider call is the hard floor under every other optimization, and hosted-US inference is the DPDP/RBI residency blocker flagged in `docs/PRODUCTION_MASTERPLAN.md` §2 (item 2) and `docs/RESIDENCY.md`. One change addresses both.

**Files:** Stubs already in place: `backend/koel/stt/providers.py` (`WhisperSageMakerProvider`, line 333), `backend/koel/tts/providers.py` (`OrpheusSageMakerProvider`), `backend/koel/nlp/rag.py` (`BedrockTitanEmbedder`, line 98 — note: Titan is 1024-d compatible), `backend/koel/nlp/providers.py` (new `SarvamNlpProvider` next to `GroqNlpProvider`), all `*/config.py` provider switches.

**Approach:** Two tracks, decided by the masterplan Phase-0 legal ruling, in cost order. **Track A (cheap):** add `SarvamNlpProvider` implementing the existing `NlpProvider` ABC against Sarvam chat completions (reuse the `koel/common/http.py` client and the JSON-mode prompt from `GroqNlpProvider`), making Sarvam the full STT+NLP+TTS stack for hi/hinglish and evaluating it for en — config-only rollout via the existing language-routing seam. **Track B (thorough):** flesh out the SageMaker/Bedrock stubs in `ap-south-1`; this is mostly infra (GPU provisioning, model licensing) and belongs to masterplan Phases 0–2, with the code side being just completing the stubs (see item **6.8**). Keep Groq as the out-of-region fallback behind the residency flag.

**Gain:** −250ms+ per provider round-trip → true first-audio p50 est. 0.4–0.6s from India; unblocks the residency launch blocker. This is the only item that moves the physical floor.

**Dependencies:** Legal residency ruling (masterplan, pre-Phase-0); Sarvam hosting/DPA verification; NLP quality eval of Sarvam vs Groq on the gold set before cutover.

### O-8 · Redis-backed rate limiter — S

**What:** Replace the in-memory per-process `RateLimiter` with a Redis sliding-window/token-bucket.

**Why:** `common/ratelimit.py` state lives in one worker's memory: limits multiply by worker count and reset on deploy — wrong the moment `uvicorn --workers >1` or a second Render instance exists.

**Files:** `backend/koel/common/ratelimit.py` (keep `rate_limit()` dependency signature), Redis client wiring (session store already uses Redis — reuse its connection config from `voicebot/session/store.py` / `common/config.py`).

**Approach:** Add a Redis implementation behind the same `check(key)` interface using `INCR`+`EXPIRE` (fixed window) or a small Lua token bucket; select via settings with in-memory as the dev/test fallback so the FastAPI dependency call-sites don't change.

**Gain:** Correctness, not latency. Prerequisite for any horizontal scaling in masterplan Phase 2.

**Dependencies:** Redis reachable from all services (already declared in `render.yaml`).

### O-9 · Per-call unit economics + budget caps — M

**What:** Extend the existing `koel.nlp.cost_usd` counter (`common/telemetry.py:126`) to full per-turn STT+LLM+TTS+embedding cost, aggregate per session, enforce per-session/per-org budget caps, and emit a provider cost-comparison view.

**Why:** Masterplan domain #18 flags unit economics as a gap; with O-7 introducing provider choice (Sarvam vs Groq vs self-hosted), cost-per-call becomes a routing input, and an unbounded LLM loop is a spend incident waiting to happen.

**Files:** `backend/koel/common/telemetry.py` (generalize `record_nlp_usage` → `record_provider_usage(service, provider, units, cost)`), `backend/koel/nlp/providers.py` (`_record_usage`, line 392 — the pattern to replicate), `backend/koel/stt/providers.py`, `backend/koel/tts/providers.py`, `backend/koel/voicebot/session/models.py` (running cost on `SessionState`), `backend/koel/voicebot/pipeline/orchestrator.py` (cap check → polite wrap-up via existing `end_call`).

**Approach:** Each provider records units (audio-seconds / tokens / characters) with a static price table in config; the orchestrator accumulates onto session state and, on breach of a configurable cap, routes through the existing `end_call`/escalation path instead of hard-cutting. Dashboarding of the comparison belongs to masterplan Phase 1 observability wiring — emit the metrics here, wire collectors there.

**Gain:** Cost-per-call number for pricing; hard ceiling on per-session spend; data to choose providers in O-7.

**Dependencies:** None; dashboard visibility depends on masterplan Phase 1.

### O-10 · DB pool & retrieval tuning — S

**What:** Right-size `pool_size`/`max_overflow` (`backend/koel/db/session.py:30`) and validate pgvector HNSW query plans at target concurrency.

**Why:** Current 10+5 per service is fine at 10 concurrent sessions but each turn does session persistence + RAG lookups; at the 50-concurrent masterplan target, pool exhaustion shows up as tail latency. HNSW index already exists, so this is verification + knobs, not schema work.

**Files:** `backend/koel/db/session.py`, `backend/koel/common/config.py` (pool settings), `backend/koel/nlp/rag.py` (`retrieve()` query).

**Approach:** Load-test at 50 concurrent (masterplan Phase 2 owns the harness), watch pool checkout wait via SQLAlchemy events + OTel, and bump pools/`hnsw.ef_search` accordingly; confirm `retrieve()` uses the index (EXPLAIN) with the org/index filters applied. Pure measurement-then-knobs.

**Gain:** Flat p95 under 5× current load; likely no user-visible change at today's traffic.

**Dependencies:** Masterplan Phase 2 load-test environment.

**Latency sequencing:** O-1 → O-2 → O-3/O-4 (parallel) → O-5 → O-6, with O-7 gated on the masterplan residency ruling and pursued as Track A (Sarvam) unless legal forces Track B. O-8/O-9/O-10 slot in before masterplan Phase 2 scale-out.

---

# Part B — P1 · Code Quality & Engineering Enablers

## Priority Index

| # | Item | Effort | Why / Approach |
|---|------|--------|-----------------|
| Q-1 | App.tsx decomposition (3,564 lines → modules) | M | Unblocks all FE work; enables code-split |
| Q-2 | JWT persistence + silent refresh | S | No logout on reload; uses existing backend seam |
| Q-3 | Wire dashboard/config to live data; fix false copy | S | Removes misleading "276ms P95" + "Bedrock" claims |
| Q-4 | TypeScript strict mode | M | Catches real bugs pre-merge |
| Q-5 | CI gate hardening | S | Regressions actually block merge |
| Q-6 | render.yaml / Dockerfile reconciliation | S | Deploy config matches reality |
| Q-7 | E2E expansion (4 tests, chromium-only → call-flow matrix) | M | Coverage of the actual product surface |

### Q-1 · App.tsx decomposition — M

**What:** Split the 3,564-line `src/App.tsx` into route/view modules and hooks.

**Why:** Every FE change currently merge-conflicts in one file; no code-splitting; strict migration (Q-4) is intractable against one 3.5k-line file.

**Files:** `src/App.tsx` → `src/views/LoginView.tsx` (lines 792–880), `src/views/DashboardView.tsx` (~940–1072), `src/views/ActiveCallView.tsx` + `src/hooks/useLiveCall.ts` (1128–2760 — the WS/audio/VAD state machine is the valuable extraction), `src/views/ConfigView.tsx` (2784–3021), `src/views/BotBuilderView.tsx` (3047–3406), `src/views/KnowledgeBaseView.tsx` (3411+), `src/lib/mockData.ts` (183–421).

**Approach:** Mechanical extraction in the order above, one view per PR, props-first (no state-library adoption in this pass — that's a separate decision); `useLiveCall` absorbs the WS lifecycle, `src/lib/vad.ts` integration, barge-in and playback state so `ActiveCallView` becomes presentational. Add `React.lazy` per view at the end for code-split. No behavior change; e2e suite (Q-7) is the safety net.

**Gain:** ~3,500 → ~7 files under 600 lines; unblocks Q-2/Q-3/Q-4; initial bundle shrinks (dashboard users never load the call view).

**Dependencies:** None, but land before Q-4 and before any new FE features.

### Q-2 · JWT persistence + silent refresh — S

**What:** Persist the access token and use the already-built `POST /v1/auth/refresh` (`backend/koel/voicebot/api/auth.py:67`).

**Why:** JWT lives only in React state — every reload logs the operator out mid-shift. The backend config comment (`common/config.py:53`) even documents the intended flow ("the dashboard silently refreshes via /v1/auth/refresh"); the FE just never implemented it.

**Files:** `src/App.tsx` login/token state (extracted to `src/hooks/useAuth.ts` under Q-1), `backend/koel/voicebot/api/auth.py` (reference only).

**Approach:** Store the refresh token (httpOnly cookie if the backend sets one; otherwise localStorage with the known XSS trade-off documented), keep the short-TTL access token in memory, and add a fetch wrapper that on 401 calls `/v1/auth/refresh` once and retries. Timer-based proactive refresh at ~80% of TTL matches the backend's short-access-TTL design.

**Gain:** No logout on reload; matches the backend's existing security design instead of fighting it.

**Dependencies:** Easier after Q-1 (or do inside `useAuth` extraction as its first slice).

### Q-3 · Wire dashboard/config to live data; kill false copy — S

**What:** Replace hardcoded/false UI claims with live values.

**Why:** The dashboard KPI strip is hardcoded (`src/App.tsx:993–996`), including **"Turn latency · P95 276ms — Within &lt;300 ms budget"** — directly contradicted by the measured 2.1–2.3s p95; `getAllStats` already exists and is unused. `ConfigView` doesn't load live bot config (line 2786) and shows a false "Draft" badge (2832); Builder preflight is literal `ok: true` (3071–3076); "Bedrock" appears in user-facing copy 3× (1656, 2123, 3037) though NLP is Groq.

**Files:** `src/App.tsx` (all refs above), backend stats/config endpoints (reference: whatever `getAllStats` calls; bot config GET already serves the builder).

**Approach:** Swap the KPI literals for a `getAllStats` fetch with loading/error states; have `ConfigView` fetch current bot config on mount and derive the Draft badge from actual state; make preflight checks call real endpoints (KB reachability = existing KB index endpoint; variable mapping = client-side check against fetched CRM schema); s/Bedrock/Groq/ in copy. Each is a small, independent fix — batch into one "truthful UI" PR.

**Gain:** Demo/UI stops asserting an SLO the system doesn't meet — this is a credibility bug, not cosmetics.

**Dependencies:** None (cleaner post-Q-1).

### Q-4 · TypeScript strict mode — M

**What:** Flip `"strict": false` → `true` in `tsconfig.json` (line 14) and fix fallout.

**Why:** The WS protocol handling, audio buffers, and VAD glue in the call path are exactly the code where implicit-any hides real bugs.

**Files:** `tsconfig.json`, then errors across `src/` (concentrated in the extracted `useLiveCall`/protocol code).

**Approach:** Post-Q-1, enable per-flag incrementally (`noImplicitAny` → `strictNullChecks` → full `strict`), fixing one extracted module per PR; type the WS message union against `backend/koel/voicebot/ws/protocol.py` message shapes so FE/BE drift becomes a compile error. Gate in CI once green (ties to Q-5).

**Gain:** Compile-time capture of null/undefined and protocol-drift bugs in the live-call path.

**Dependencies:** Q-1 (do not attempt against the monolith).

### Q-5 · CI gate hardening — S

**What:** Make CI actually fail on regressions.

**Why:** All gates are advisory: `mypy koel || true` (ci.yml:45), `pip-audit || true` (:51), `npm audit || true` (:66), Trivy `exit-code: "0"` (:84), no `--cov-fail-under`, e2e is 4 tests on chromium only.

**Files:** `.github/workflows/ci.yml`, `playwright.config.ts`, `backend/pyproject.toml` (or pytest args).

**Approach:** Add `--cov-fail-under=85` (current ~90% per masterplan, leave headroom); flip Trivy to `exit-code: "1"` scoped to CRITICAL/HIGH with an ignore file for accepted findings; graduate mypy module-by-module via a `mypy.ini` per-module `ignore_errors` allowlist that only shrinks; keep `npm audit`/`pip-audit` advisory but publish as PR annotations so they're seen. Add firefox+webkit projects to Playwright config (cheap, config-only).

**Gain:** Regressions block merge instead of scrolling past; coverage can no longer silently decay.

**Dependencies:** mypy graduation is ongoing; everything else is one PR.

### Q-6 · render.yaml / Dockerfile reconciliation — S

**What:** Make deploy config describe the deployment that actually exists.

**Why:** `render.yaml` declares 5 starter services + Redis + Postgres (lines 16–63); the live deploy actually runs one free-tier service running `koel/combined.py` from `main` — three mutually inconsistent stories (also see the Corrections Appendix below). First real deploy or new-hire onboarding hits this immediately.

**Files:** `render.yaml`, `backend/Dockerfile`, `backend/koel/combined.py`, `docs/DEPLOY.md`.

**Approach:** Pick the combined single-service topology as the documented default (it's what's measured and live): render.yaml gets one web service with `dockerCommand: uvicorn koel.combined:app...`, Dockerfile CMD matches, and the 5-service split moves to a clearly-labeled commented block or `render.split.yaml` for the masterplan Phase-2 scale-out. Update DEPLOY.md to match.

**Gain:** `render.yaml` becomes executable truth; removes a guaranteed first-deploy failure.

**Dependencies:** None.

### Q-7 · E2E expansion — M

**What:** Grow from 4 chromium-only dashboard tests (`e2e/dashboard.spec.ts`) to a call-flow matrix.

**Why:** The product is the live call loop; zero e2e coverage exists for WS session, greeting audio, barge-in, language switch, or escalation — the flows every latency optimization above touches.

**Files:** `e2e/` (new specs), `playwright.config.ts`, `src/lib/vad.ts` (test seam for synthetic audio), backend stub providers (already exist: `StubWhisperProvider`, `StubNlpProvider`, `StubOrpheusProvider` — the e2e backend can run fully stubbed with deterministic latency).

**Approach:** Run the backend in stub-provider mode in CI (no API keys, deterministic), inject synthetic mic audio via a Playwright-exposed test hook around the VAD, and assert on the WS message sequence (`ws/protocol.py` types) plus audio-element state: happy-path turn, barge-in cancels TTS, unsupported-language escalation, session resume after reload (pairs with Q-2). Cross-browser via the Q-5 config change.

**Gain:** Regression net for O-1/O-2/O-6, which all rewrite the turn loop; 4 → ~15–20 meaningful tests.

**Dependencies:** Q-5 (browsers), benefits from Q-1's `useLiveCall` seam.

**Quality sequencing:** Q-6 + Q-5 + Q-3 are one sprint of cheap credibility fixes; Q-1 → Q-2/Q-4 → Q-7 is the structural arc.

---

# Part C — P2 · Features & Integrations

Six themes, roughly ordered by how directly each converts the existing MVP into
revenue/breadth. Every item is a genuine gap against the PRD (§04 in-call
actions, §10 post-MVP list) with a concrete reuse path over an already-built
seam.

## Theme 1 — Telephony & Outbound Scale *(the demo→revenue jump)*

### 1.1 Twilio Media Streams PSTN bridge — L

**What:** A bridge service that terminates a Twilio Media Streams WebSocket (bidirectional 8 kHz µ-law) and drives the existing orchestrator, taking KOEL from browser demo to real phone calls.

**Why:** PRD §10 "production telephony" — the single biggest gap between the product and revenue; every downstream outbound feature depends on it.

**Files:** new `backend/koel/voicebot/telephony/` package (`bridge.py`, `twilio_ws.py`, `webhooks.py`); `backend/koel/tts/mixer.py` (`to_ulaw_8k`, line 146 — done); `backend/koel/tts/providers.py` (`RoutingTtsProvider._maybe_telephony` — done); `backend/koel/voicebot/pipeline/orchestrator.py` (no change: transport-agnostic `send` callback); `backend/koel/voicebot/ws/handler.py` (reference implementation to mirror); `docs/TELEPHONY.md` (spec already written).

**Approach:** Follow `docs/TELEPHONY.md` exactly — it already enumerates the five missing pieces (Media Streams WS server, webhook signature validation, inbound µ-law→PCM decode, DTMF, dialer). The bridge is a second WS endpoint that adapts Twilio `media`/`mark`/`clear` events to the same session lifecycle the browser handler drives; outbound audio flows through the existing `SynthesizeRequest.telephony=True` + `KOEL_TELEPHONY_MODE` seam so no TTS code changes. Inbound µ-law frames are decoded to 16 kHz PCM and posted to the existing batch STT; barge-in reuses the two-gate classifier with a server-side energy VAD replacing the browser Silero VAD (the adaptive noise floor logic in `pipeline/bargein.py` ports directly). The grace-TTL dropped-call finalizer is already WS-transport-scoped so PSTN legs won't be mis-finalized.

**Dependencies:** Twilio account + Indian DID or Exotel (see 1.4); webhook signature validation and recording-bucket SSE are Masterplan launch blockers, not duplicated here.

### 1.2 Outbound campaign dialer + retry scheduling — M

**What:** Campaign objects (bot + customer segment + window), a dial worker with per-campaign concurrency caps, and retry scheduling on `busy`/`no_answer`/`call_dropped`.

**Why:** PRD §05 describes campaign-driven outbound as the core loop; today calls start one at a time via `POST` in `backend/koel/voicebot/api/sessions.py`.

**Files:** new `backend/koel/voicebot/campaigns/` (models, scheduler, worker); `backend/koel/db/models.py` (Campaign, CampaignMember, DialAttempt tables); `backend/koel/voicebot/api/` new `campaigns.py` router; reuse `enforce_calling_hours` (calling-hours IST logic) and DND scrub already enforced at session start.

**Approach:** An asyncio worker loop (same process, no new infra) claims due `DialAttempt` rows with `FOR UPDATE SKIP LOCKED`, checks calling hours/DND via the existing gates, and originates via the 1.1 bridge's Twilio REST call. Retry policy is a per-campaign JSON (max attempts, backoff, outcome→action map) stored alongside `BotConfig.config` conventions. `CallLog.outcome` already captures everything the retry decision needs. CRM `"retry"`/`"callback"` flags (already in `crm/schemas.py FlagRequest`) feed re-queueing.

**Dependencies:** 1.1. Delivers PRD §10 "EC2 auto-scaling" inputs (queue depth metric) but scaling itself stays in the Masterplan.

### 1.3 DTMF capture — S

**What:** Handle Twilio `dtmf` events as a first-class input alongside STT ("press 1 to confirm").

**Why:** docs/TELEPHONY.md item 4; required for OTP-style confirmation in collections/insurance and for 3.2 IVR replacement.

**Files:** `backend/koel/voicebot/telephony/twilio_ws.py` (from 1.1); `backend/koel/voicebot/pipeline/orchestrator.py` (accept a synthetic `Turn` whose text is `"<dtmf:1>"` or map digits to intents); `backend/koel/voicebot/session/models.py`.

**Approach:** Translate DTMF events into the same customer-turn path STT results take, tagging `TranscriptTurn.intent` directly and bypassing NLP when a bot's config declares a digit map. Two-sentence change to the orchestrator's turn intake plus a `BotConfig.config["dtmf_map"]` convention.

**Dependencies:** 1.1.

### 1.4 Exotel carrier adapter — M *(second priority behind Twilio)*

**What:** Same bridge contract against Exotel's voice streaming API for Indian cost/latency/compliance advantages.

**Why:** PRD §10 names "Twilio/Exotel" explicitly; Indian PSTN termination via Exotel is materially cheaper and keeps media in-country (pairs with `docs/RESIDENCY.md`).

**Files:** `backend/koel/voicebot/telephony/` — define a `CarrierBridge` protocol in `bridge.py` when building 1.1 so Exotel is a second implementation, not a fork.

**Approach:** Extract the Twilio-specific WS framing behind a small adapter interface (connect, on_media, send_media, send_clear, on_dtmf, hangup) during 1.1 code review, then implement Exotel's equivalent. Everything above the adapter (orchestrator, STT/TTS, barge-in) is shared.

**Dependencies:** 1.1 shipped and stable.

## Theme 2 — In-Call Revenue Actions (PRD §04 gaps)

### 2.1 Payment-link SMS dispatch + promise-to-pay capture — M

**What:** During a collections call, send a Razorpay/Setu payment link by SMS on customer agreement, and record a structured PTP (amount, date) on the CRM record.

**Why:** PRD §04 collections — the explicit MVP gap; PTP rate is *the* collections KPI, and the vocabulary already exists (`"promise_to_pay"` is a named event in `crm/schemas.py InteractionLog`).

**Files:** new `backend/koel/common/sms.py` (provider client: Twilio SMS or MSG91); `backend/koel/nlp/main.py` + prompts (add a `payment_link_requested` / `ptp` structured field to the JSON-mode response schema); `backend/koel/voicebot/pipeline/orchestrator.py` (action dispatch on NLP result); `backend/koel/crm/main.py` (`POST /{customer_id}/interactions` already idempotent — reuse); `backend/koel/db/models.py` (optional `PromiseToPay` table or reuse `InteractionLogRow.detail`).

**Approach:** Extend the Groq JSON-mode NLP contract with an optional `actions` array (e.g. `{"type": "send_payment_link"}` / `{"type": "ptp", "amount": ..., "date": ...}`) — the LLM already runs in JSON mode so this is a prompt + schema change, not an architecture change. The orchestrator executes actions after sending the bot turn, writing through the existing idempotency-keyed CRM interaction path so retries never double-send an SMS (generate the idempotency key from session_id + turn index). Link generation is a thin payment-provider client; the bot confirms dispatch verbally in the same turn ("maine link bhej diya hai"). PTP rows surface in 6.2 analytics as a funnel stage.

**Dependencies:** SMS provider account + DLT template registration (India) — flag as an ops prereq, procurement tracked in Masterplan.

### 2.2 Dynamic EMI calculation — S

**What:** Compute real EMI quotes in-call from principal/tenure/rate instead of scripted numbers, for the personal-loan use case.

**Why:** PRD §04 personal loan gap; LLMs are unreliable at arithmetic, so a deterministic tool is required for a regulated quote.

**Files:** new `backend/koel/nlp/tools.py` (pure `emi(principal, annual_rate, months)` function); `backend/koel/nlp/main.py` (`POST /v1/respond` — detect an `emi_request` action in the JSON response, compute server-side, and re-render the reply with the computed figures before returning); `backend/koel/crm/seed.py` (customer `attributes` already carries `emi`/offer fields — extend seeds).

**Approach:** Rather than full function-calling round-trips (latency-hostile on the voice path), have the NLP prompt emit a slot-filled template plus extracted parameters; the service computes EMI deterministically and substitutes into the reply text before TTS. `CustomerRecord.attributes` already carries the offer parameters, so no CRM schema change. Log the computed quote to the interaction log for audit.

**Dependencies:** none.

### 2.3 Verbal-consent recording — S/M

**What:** A consent capture flow for insurance renewal: bot reads the disclosure, captures an explicit "yes", and stores a pointer to the exact recording offsets plus transcript turn as evidence.

**Why:** PRD §04 insurance gap; IRDAI expects verifiable verbal consent. `CallLog.metrics` already holds a "consent log" rollup — this makes it evidentiary.

**Files:** `backend/koel/voicebot/pipeline/orchestrator.py` (a consent sub-state: disclosure sent → affirmative intent required verbatim); `backend/koel/voicebot/recording.py` (S3 upload exists — add turn-offset metadata); `backend/koel/db/models.py` (`ConsentRecord`: call_id, turn_id, recording_url, offset_ms, disclosure_version); `backend/koel/crm/main.py` (append consent event).

**Approach:** Model consent as an orchestrator sub-state entered by a bot-config flag: the disclosure turn is marked non-interruptible (barge-in gate already supports suppression via the dual-gate design), and only a high-confidence affirmative intent exits it. Persist a `ConsentRecord` linking the `TranscriptTurn` and the S3 recording with time offsets computed from the existing per-turn timing metrics. Feeds 6.5 audit exports directly.

**Dependencies:** S3 bucket/SSE/lifecycle (Masterplan launch blocker).

### 2.4 WhatsApp follow-up — M

**What:** Post-call WhatsApp message via Twilio WhatsApp / Meta Cloud API: offer summary, payment link, callback confirmation — templated per outcome.

**Why:** WhatsApp open rates in India dwarf SMS; PRD §04's cross-sell flow ends with "details follow" which currently goes nowhere.

**Files:** `backend/koel/common/sms.py` from 2.1 generalized to `messaging.py` (channel enum: sms/whatsapp); `backend/koel/voicebot/ws/handler.py` `_finalize` (dispatch after summary, same place `SarvamSummarizer` runs); `backend/koel/db/models.py` (message dispatch log for dedupe); template mapping in `BotConfig.config["followup_templates"]`.

**Approach:** Hook the existing `_finalize` post-call path: after the summarizer produces `CallSummary`, a follow-up policy maps `outcome` + `next_action` to a pre-approved WhatsApp template and fires it through the messaging client with an idempotency key of session_id. Off the critical path by construction, mirroring the summarizer's placement. Delivery receipts land in a webhook that updates the dispatch log for 6.2 analytics.

**Dependencies:** 2.1 messaging client; WhatsApp Business Account + template approval (ops prereq).

### 2.5 Scheduled callbacks — S

**What:** When the customer says "call me tomorrow at 5", parse the time, confirm it, and enqueue a dial attempt.

**Why:** The `"callback"` flag already exists in `FlagRequest` but is a dead end; converting it to a scheduled dial is nearly free once 1.2 exists and directly recovers otherwise-lost conversions.

**Files:** `backend/koel/nlp/main.py` (extract `callback_at` in the JSON response, IST-aware); `backend/koel/voicebot/pipeline/orchestrator.py` (confirm verbally, write flag with parsed time in `detail`); `backend/koel/voicebot/campaigns/scheduler.py` (from 1.2 — consume callback flags into `DialAttempt`s, clamped to calling hours).

**Dependencies:** 1.2.

## Theme 3 — Human-in-the-Loop & Inbound

### 3.1 Real human-escalation handoff — DONE

**Status (updated):** Both phases are built. Phase A (browser, no carrier): `_escalate` parks the call in `waiting_agent` playing hold filler, pushes the context payload onto the `escalation_requests` queue (`backend/koel/voicebot/persistence.py`), and a logged-in operator claims/replies/resolves via `backend/koel/voicebot/api/operators.py` — replies go through the existing TTS path. `EscalationHub`/`LiveMonitor` (see 3.3) are Redis-backed so this now also works across multiple worker processes/instances, not just one.

Phase B (PSTN) shipped via Twilio's **Conference API**, not TaskRouter — TaskRouter is a routing/queueing product needing a provisioned Workspace/Workflow/TaskQueue that no live Twilio account here could set up or verify against; a Conference-based warm transfer (redirect the live call + a new outbound whisper call into one ad-hoc conference room) gets the identical real-world outcome — operator ends up on the phone with the customer — through a smaller, stable, well-documented REST surface. See `backend/koel/voicebot/pipeline/warm_transfer.py` and `POST /v1/operator/escalations/{id}/warm-transfer`. Needed capturing the Twilio CallSid at all (added: `SessionState.twilio_call_sid`, set from the Media Streams start frame — nothing captured it before).

**Not done:** `escalation_resolved`/`escalation_abandoned` as distinct `CallOutcome` values for 6.2 — the queue-side `EscalationStatus` enum already has `resolved`/`abandoned`, but `CallOutcome` itself still just uses `escalated`.

### 3.2 Inbound Koel_bot (intent detection / self-service / IVR replacement) — PARTIALLY DONE

**Status (updated):** The core inbound path works: `/twilio/stream` resolves an inbound leg with no `attempt_id`/`bot_id` query params via the ANI (caller-number) lookup path (`backend/koel/voicebot/inbound.py`), binds it to `twilio_default_bot_id`, and seeds an intent-first session (`DIRECTION_KEY` in `crm_context`, read by `koel.nlp.providers.select_system_prompt`). DTMF-as-IVR (1.3) works via `TwilioMediaBridge._on_dtmf` → `dtmf_to_turn`.

**Not done:** "smart escalation" — `BotConfig.config["escalation_rules"]` is a reserved column comment only; nothing reads it (`grep -rn escalation_rules koel` → zero hits), so there's no intent/sentiment-based auto-escalation routing. Also no dedicated `inbound_support` `UseCase` — inbound reuses whichever bot `twilio_default_bot_id` points at, not a bot-type-scoped behavior split.

**Dependencies (remaining):** the escalation-rules routing logic itself; a decision on whether a dedicated bot type is actually needed or whether the current generic-bot-plus-direction-flag approach is sufficient.

### 3.3 Live supervisor monitor — DONE

**Status (updated):** `backend/koel/voicebot/monitor.py`'s `LiveMonitor` + `GET /v1/operator/monitor` / `/v1/operator/monitor/{session_id}` ship a read-only live view (status, turn count, recent transcript events), org-scoped. Now Redis-backed (optional `redis_client`) so it reflects calls served by any worker/instance, not just the process the request happened to land on — in-process by default when unconfigured, matching every other cross-instance feature in this codebase.

**Not done:** a live "barge/claim" action folding directly into the browser dashboard's supervisor view (the API exists; no dedicated FE supervisor screen was built — `AdminView`/`operators` API consumption from a supervisor-specific UI is still open if wanted).

## Theme 4 — Language & Voice Expansion

### 4.1 More Indian languages via Sarvam (ta / te / mr / bn first) — M

**What:** Extend beyond EN/HI/Hinglish using Sarvam bulbul TTS (supports ta-IN, te-IN, mr-IN, bn-IN, gu-IN, kn-IN, ml-IN, pa-IN, od-IN) and Groq Whisper's multilingual STT.

**Why:** PRD positions KOEL for Indian financial services; Tamil/Telugu/Marathi/Bengali cover the next ~40% of the market. The plumbing is already language-routed end to end.

**Files:** `backend/koel/common/schemas.py:27` (`Language` enum — add members); `backend/koel/tts/providers.py:354` (`SarvamTtsProvider._LANG_CODE` — add mappings; today only `hi/hinglish/en`) and `RoutingTtsProvider` (line 512 — rename the `hindi_languages` routing set to a general Sarvam-languages set); `backend/koel/stt/` (language pinning table — Whisper supports all four); `backend/koel/nlp/` prompts + `pipeline/localization.py` (per-language system text, fallback lines); `backend/koel/voicebot/pipeline/transliteration.py` (script handling is Devanagari-specific — Tamil/Telugu/Bengali scripts need per-language treatment or bypass); FE language picker + `crm/seed.py`.

**Approach:** This is deliberately a config/tables change, not new architecture: every hop (STT pin, NLP prompt selection, TTS routing, localization strings, language-fallback detection) keys off the `Language` enum, so add one language at a time and walk the enum's consumers (grep for `Language.` exhaustively — the unsupported-language escalation floor in the orchestrator must also whitelist new members). Sarvam WS streaming and smart-turn endpointing are language-agnostic and carry over. Seed one RAG index per language per the existing 3-index pattern, and extend the summarizer prompt to note transcript language. Budget real speaker QA per language — the Hindi dual-script lessons (see commit `8b8388e`) will recur differently per script.

**Dependencies:** native-speaker eval time; no infra.

### 4.2 Persona voices / voice cloning — S/M

**What:** Named, per-bot voice personas — Sarvam speaker selection (multiple bulbul speakers) now; Sarvam/ElevenLabs voice cloning for brand voices later.

**Why:** PRD §06 voice personality is a differentiator; today the speaker is a single constructor arg (`SarvamTtsProvider(speaker=...)`, `providers.py:361`).

**Files:** `backend/koel/tts/providers.py` (thread `speaker`/`voice` through `SynthesizeRequest` instead of constructor-fixed); `backend/koel/tts/schemas.py`; `backend/koel/db/models.py` (`BotConfig.voice_gender` exists — supersede with `voice_id`); `backend/koel/voicebot/pipeline/orchestrator.py` (pass bot's voice into synth requests); FE bot builder voice picker.

**Approach:** Make voice a per-request field with the current values as defaults, mapped from `BotConfig` — a mechanical threading change through the existing `SynthesizeRequest` seam. Cloning is then just a new `voice_id` namespace pointing at a cloned Sarvam/vendor voice, gated per org. Keep the Magpie/Orpheus fallbacks pinned to nearest-match voices so the cascade (already in `RoutingTtsProvider`) stays coherent.

**Dependencies:** none for speaker selection; vendor cloning contract + recorded consent from voice talent for cloning.

### 4.3 Sentiment-driven tone shifts (in-call) — S

**What:** Adjust the bot's `Tone` per turn from live customer sentiment, not just per-intent scripting.

**Why:** PRD §06 empathy requirements; the machinery exists — `Tone` already flows to TTS (`_escalate_unsupported_language` sends `tone=Tone.empathetic`) and the post-call summarizer already classifies sentiment, just too late to act.

**Files:** `backend/koel/nlp/main.py` (add `customer_sentiment` to the JSON-mode response schema — one prompt change); `backend/koel/voicebot/pipeline/orchestrator.py` (map sentiment trend → `Tone` for the next `_send_bot_turn`, e.g. two negative turns → empathetic + slower `speaking_rate`); `backend/koel/db/models.py` (`TranscriptTurn.tone` exists — also persist sentiment in `confidence`-adjacent column or turn meta).

**Approach:** Zero new services: the NLP call already returns structured JSON per turn, so adding a sentiment field costs no latency; a tiny pure function in the orchestrator maps a rolling window of sentiments to tone/rate adjustments. Persist per-turn sentiment so 6.2 can chart sentiment trajectories and 3.1 can route angry customers to humans faster.

**Dependencies:** none.

## Theme 5 — Conversation Intelligence

> Items 5.1 and 5.2 below are latency-critical, so the **full text also appears
> in Part A as O-2 and O-6 respectively** (identical content — cross-referenced
> here for readers going theme-by-theme rather than by ROI-part).

### 5.1 Streaming NLP (token streaming into sentence-TTS) — M *(= O-2 in Part A)*

**What:** Stream Groq completion tokens and start TTS on the first complete sentence, replacing the current buffered `POST /v1/respond` (the only NLP entry point, `backend/koel/nlp/main.py:59`).

**Why:** Biggest remaining latency lever: sentence-streaming TTS and Sarvam WS streaming already exist downstream, but they currently wait for the full LLM response before the first sentence moves.

**Files:** `backend/koel/nlp/main.py` (new `POST /v1/respond-stream`, SSE or chunked); `backend/koel/voicebot/clients.py` (streaming client); `backend/koel/voicebot/pipeline/orchestrator.py` (feed sentences to the existing sentence-streaming TTS path as they arrive; barge-in kill must cancel the NLP stream too).

**Approach:** Groq supports SSE streaming; the wrinkle is that the response is JSON-mode (intent/tone/actions + text). Split the contract: emit a small structured header event first (intent, tone — the model produces these keys first if the prompt orders them first), then stream the reply text field, sentence-segmented server-side with the same segmentation used by sentence-streaming TTS. The dual-gate barge-in kill already cancels TTS mid-turn; extend the same cancellation token to the NLP stream. Keep the batch endpoint as the fallback path (mirroring the Sarvam WS→HTTP fallback pattern at `tts/providers.py:448-450`).

**Dependencies:** none; do before 5.2 (bigger win, less risk).

### 5.2 Streaming STT — M/L *(= O-6 in Part A)*

**What:** Partial transcripts during customer speech instead of batch-REST-after-endpoint.

**Why:** Enables faster turn starts, richer barge-in classification (kill TTS on meaningful partials, upgrading the min-words gate), and live captions for 3.3.

**Files:** `backend/koel/stt/` (new streaming provider — Sarvam streaming STT (saarika) or Deepgram, behind the existing `SttProvider` abstraction); `backend/koel/voicebot/ws/handler.py` (forward audio frames continuously rather than utterance-complete); `backend/koel/voicebot/pipeline/endpointing.py` (smart-turn can then run on rolling audio, tightening the merge window).

**Approach:** Add a `StreamingSttProvider` variant next to the batch providers with the batch path as automatic fallback (same degrade-gracefully posture as endpointing's self-disabling wrapper). The browser already ships frames continuously for VAD, so the FE change is small; the handler gains an interim-transcript message type. Do this after 5.1 — batch Groq Whisper is fast enough that this is a refinement, not a blocker.

**Dependencies:** streaming STT vendor selection (Groq Whisper is batch-only today).

### 5.3 Surface call summaries in history + dashboard — S

**What:** Expose the already-generated `CallSummary` (interest/objection/sentiment/next_action) in `GET /calls/{id}` and the FE call-detail view, plus objection frequency rollups.

**Why:** The summarizer runs on every call and writes to CRM (`crm/main.py POST /call-summary`) but operators never see it; this is banked value with a UI gap.

**Files:** `backend/koel/voicebot/api/history.py` (join summary into `CallDetail`); `backend/koel/db/models.py` (persist the summary on `CallLog.metrics` or a column at `_finalize` time in `ws/handler.py`, in addition to the CRM POST); `src/App.tsx` call history view.

**Dependencies:** none. Highest value-per-effort item in this theme.

### 5.4 Per-bot LLM / prompt overrides — S

**What:** Let a bot override the NLP model tier (8b vs 70b), temperature, and system-prompt preamble.

**Why:** PRD's "new use cases are config" moat (quoted verbatim in `BotConfig`'s docstring); today model routing is language-based only, and prompt text is service-global.

**Files:** `backend/koel/db/models.py` (`BotConfig` — follow the exact `rag_top_k`/`rag_score_threshold` nullable-override pattern already there: `llm_model`, `llm_temperature`, `prompt_preamble`); `backend/koel/nlp/main.py` + schemas (accept overrides on the request); `backend/koel/voicebot/pipeline/orchestrator.py` (pass from bot config); `backend/koel/voicebot/api/bots.py` (expose in PATCH, `BotConfigAudit` captures changes for free).

**Approach:** Copy the established nullable-column-falls-back-to-service-default pattern; guard `prompt_preamble` as an *addition* to the hardened system prompt, never a replacement, to preserve the compliance/disclosure language. This also unblocks 6.3 (A/B variants are mostly prompt overrides).

**Dependencies:** none.

### 5.5 Script-adherence QA scoring — M

**What:** Post-call automated QA: did the bot make mandated disclosures, avoid prohibited phrases, follow the flow? Score per call, aggregate per bot.

**Why:** Collections in India is compliance-sensitive (RBI FPC); buyers ask "how do you audit the bot?" — this plus 6.5 is the answer.

**Files:** new `backend/koel/voicebot/pipeline/qa.py` modeled directly on `summarizer.py` (same Sarvam-chat, same `_finalize` hook, same off-critical-path placement); `backend/koel/db/models.py` (QA score on `CallLog.metrics`); rules per use case in `BotConfig.config["qa_rules"]`.

**Dependencies:** none; feeds 6.2 and 6.5.

## Theme 6 — Enterprise Platform

### 6.1 Live CRM adapter interface — M

**What:** A pluggable adapter so the voicebot reads/writes a client's real CRM (Salesforce/LeadSquared/CleverTap or plain REST) instead of the dummy service.

**Why:** PRD §10 "Live CRM integration" — first question every enterprise buyer asks. The dummy CRM's contracts are explicitly designed as the interface ("This is the interface a production CRM must satisfy" — `crm/schemas.py:1`).

**Files:** new `backend/koel/crm/adapters/` (`base.py` protocol mirroring the four existing routes: get_customer, log_interaction, set_flag, post_call_summary; `internal.py` wrapping today's DB-backed service; `rest_webhook.py` generic outbound adapter first); `backend/koel/voicebot/clients.py` (CRM client already isolates all CRM I/O — swap target by bot/org config); `BotConfig.config["crm"]` (the column comment already reserves "CRM field mapping").

**Approach:** Freeze the existing four-call contract as the adapter protocol — the orchestrator already writes through idempotency keys, so any adapter gets retry-safety for free. Ship the generic signed-webhook/REST adapter first (covers most Indian lenders' middleware), then named connectors. Field mapping lives in bot config JSON per the reserved comment; keep the internal dummy as the default and the local test double.

**Dependencies:** none for the interface + generic adapter; per-connector sandbox accounts for named CRMs.

### 6.2 Advanced analytics dashboard (funnel / outcomes) — M

**What:** Operator-facing dashboard: campaign funnel (dialed → connected → engaged → converted/PTP), outcome and objection breakdowns, sentiment trajectories, latency SLO panels — from `CallLog` + `TranscriptTurn` + summaries.

**Why:** PRD §10 "advanced analytics dashboard"; the data is already captured (`CallLog.metrics` latency rollups, per-turn intents, summarizer output) and `api/analytics.py` already proves the grouped-query posture — this extends it rather than inventing it.

**Files:** `backend/koel/voicebot/api/analytics.py` (add time-bucketed series, funnel, objection top-N endpoints — follow the existing single-grouped-query, org-scoped pattern); `src/App.tsx` (dashboard views; the FE already has a stats surface to grow); depends on 5.3 persisting summaries locally.

**Approach:** Pure read-side work: 3–4 new grouped/windowed queries over existing tables, each org-scoped exactly like `bot_stats` (including the 404-not-403 leak-prevention behavior already implemented). Funnel stages map to existing enums (`DialAttempt` from 1.2 adds the top of funnel; degrade gracefully to call-level funnel until then). Keep it SQL-native — no warehouse until row counts demand it (that call belongs in the Masterplan).

**Dependencies:** 5.3 (summary persistence); 1.2 enriches but doesn't block.

### 6.3 A/B script testing — M

**What:** Per-bot variant experiments: greeting/pitch/prompt variants with deterministic assignment and per-variant conversion readouts.

**Why:** PRD §10; conversion-rate optimization is the product's economic argument, and 5.4 + 6.2 supply both levers and measurement.

**Files:** `backend/koel/db/models.py` (`Experiment`, variant weights; stamp `variant_id` into `CallLog.metrics`); `backend/koel/voicebot/session/` (assign variant at session start — hash(customer_id, experiment_id) for determinism across retries); `backend/koel/voicebot/pipeline/orchestrator.py` (variant overrides resolve exactly like 5.4's per-bot overrides, one more layer in the same fallback chain); `backend/koel/voicebot/api/` experiments router + FE readout using 6.2's endpoints.

**Approach:** Model a variant as a partial `BotConfig` override (greeting_template, prompt_preamble, tone, voice) so the orchestrator needs no new concepts — resolution order becomes variant → bot → service default. Report per-variant `BotStats` by adding a `variant_id` group-by to the existing analytics query. Include a minimum-sample guard in the readout, not auto-promotion (keep humans in the loop for regulated scripts; `BotConfigAudit` logs promotions).

**Dependencies:** 5.4, 6.2.

### 6.4 Multi-tenant enablement — M

**What:** Promote the nullable `org_id` seam (already on `BotConfig`, `CallLog`, `KnowledgeChunk`, users at `db/models.py:87/109/138/193`) to enforced tenancy: an `orgs` table, org-scoped users, non-null backfill, and org admin APIs.

**Why:** PRD §10 "multi-tenant management" — prerequisite to onboarding a second client; enforcement is partially proven already (analytics and stats endpoints scope by `CurrentUser.org_id` today).

**Files:** `backend/koel/db/models.py` (Org table; migrate org_id → non-null with a default org backfill); `backend/koel/common/auth.py` (org claims in JWT — `CurrentUser.org_id` exists); every list/detail router in `backend/koel/voicebot/api/` (`bots.py`, `history.py`, `knowledge.py`, `sessions.py` — apply the exact scoping-plus-404 pattern from `analytics.py`); `backend/koel/crm/main.py`.

**Approach:** The seam design means this is a sweep, not a redesign: add the org table, backfill a `default` org, flip columns non-null, then audit every query for a `WHERE org_id` using `analytics.py:86-115` as the reference implementation (it already documents the anti-enumeration 404 posture). Add per-org rate limits and per-org RAG index namespacing (KnowledgeChunk has org_id already). Write cross-tenant-leak tests as the acceptance gate — extend the existing test suite's auth fixtures.

**Dependencies:** none technically; do before any second-client onboarding. Billing/quota is out of scope here (Masterplan).

### 6.5 Regulatory audit-log exports — S

**What:** Signed, immutable-ish export bundles per date range/org: call metadata, transcripts, consent records, disclosures made, config-change audits — CSV/JSONL + manifest hash.

**Why:** PRD §10 "regulatory audit log exports"; RBI/IRDAI audits are a sales objection today. Everything needed is already persisted (`CallLog`, `TranscriptTurn`, `BotConfigAudit`, `InteractionLogRow`, 2.3's `ConsentRecord`).

**Files:** new `backend/koel/voicebot/api/exports.py` (async export job → S3 presigned URL, reusing `recording.py`'s S3 client); `backend/koel/db/models.py` (ExportJob row for traceability — who exported what, itself audit-relevant).

**Approach:** A streaming query over the existing tables serialized to JSONL, zipped with a SHA-256 manifest, uploaded via the existing S3 path, returned as a presigned URL; org-scoped and role-gated. No new data capture — this is pure egress plumbing over banked records.

**Dependencies:** S3 bucket setup (Masterplan); 2.3 enriches insurance exports.

### 6.6 Conversation flow editor — L

**What:** Make the FE flow visualization (static `FLOW_NODES`/`FLOW_BRANCHES`, `src/App.tsx:3035-3046`) real: per-bot editable flow graphs (nodes, intent-keyed branches, node scripts) that the orchestrator honors.

**Why:** PRD's config-not-code moat, fully realized; `BotBuilderView` already has the node-script editing UI (`nodeScript` state with a Hinglish template) — it just persists nothing.

**Files:** `backend/koel/db/models.py` (flow JSON in `BotConfig.config["flow"]` — schema: nodes, intent→edge map, per-node script templates with `{variable}` slots); new `backend/koel/voicebot/pipeline/flow.py` (interpreter: given state + NLP intent, pick next node; node script becomes the prompt scaffold for that turn); `backend/koel/voicebot/pipeline/orchestrator.py` (consult the interpreter when a bot has a flow; fall back to today's freeform behavior when absent); `src/App.tsx` (wire the existing builder UI to `PATCH /bots/{id}`); `backend/koel/voicebot/api/bots.py` (flow validation: no unreachable nodes, all intents mapped — the FE already displays "0 unmapped variables", make it true).

**Approach:** Deliberately *not* a visual programming runtime: the flow constrains and scaffolds the LLM (node script = turn-level prompt template, branches keyed on the intent the NLP already returns per turn) rather than replacing it, so all resilience/barge-in/endpointing behavior is untouched. Ship interpreter + API first with the current default flow expressed as data; graph-editing UI second. `BotConfigAudit` versions every flow change for free.

**Dependencies:** 5.4 (prompt scaffolding mechanics); benefits from 6.3 for flow-level A/B later.

### 6.7 Real Cloudflare Turnstile — S

**What:** Replace the simulated Turnstile widget (`src/App.tsx:850-860`, "Cloudflare Turnstile" label with no verification) with the real challenge + server-side siteverify on login.

**Why:** The login page advertises protection it doesn't have; the auth endpoints have rate limits (`_login_limit`) but no bot-challenge.

**Files:** `src/App.tsx` (real widget script + token into the login POST); `backend/koel/voicebot/api/auth.py` (`/login` — verify token against `https://challenges.cloudflare.com/turnstile/v0/siteverify` before password check, using the existing `make_async_client`); config for site/secret keys.

**Dependencies:** Cloudflare site registration (free tier fine).

### 6.8 AWS-residency provider completion (SageMaker Whisper/Orpheus, Bedrock Titan) — MOSTLY DONE

**Status (updated):** `BedrockTitanEmbedder` (`backend/koel/nlp/rag.py`) is fully implemented — real `boto3 bedrock-runtime` `invoke_model` against `amazon.titan-embed-text-v2:0`, wired into `build_embedder()`, degrades to the hash fallback on any failure (mirrors `NvidiaEmbedder`'s convention). No longer a stub, no longer in `_UNIMPLEMENTED_EMBEDDERS`.

`WhisperSageMakerProvider` (`backend/koel/stt/providers.py`) and `OrpheusSageMakerProvider` (`backend/koel/tts/providers.py`) now implement the real boto3 SDK mechanics (client construction, `invoke_endpoint`, `asyncio.to_thread` offloading) — but the request/response **payload shape is an assumed convention, clearly flagged in each class's docstring**, since no SageMaker endpoint has ever been deployed to verify the real contract against. Genuinely done once a real model artifact exists: the SDK plumbing won't need to change, only the payload mapping if the deployed model's actual I/O differs from the documented assumption.

**Remaining:** provisioning the actual SageMaker endpoints (see `infra/terraform/main.tf`'s closing comment — deliberately NOT added there either, for the same "no real model artifact to point at" reason) — this needs a trained/packaged Whisper/Orpheus model pushed to S3/ECR before there's anything for Terraform or these providers to actually invoke. Also still open: the embedding-dimension re-ingestion plan if a future embedder ever changes `EMBEDDING_DIM` (Titan v2 is already 1024-d, matching today's default, so no migration was needed for Bedrock specifically).

**Dependencies:** AWS account (Masterplan procurement) + a real Whisper/Orpheus model artifact for the SageMaker half; Bedrock half has no further dependency (real AWS credentials + IAM permission — see `infra/terraform/main.tf`'s `bedrock_invoke` policy — is all it needs).

---

# Cross-Track Wave Plan (latency-first ordering)

One merged build order across all three parts, reflecting the "latency & cost
first" north star:

| Wave | Contents | Rationale |
|------|----------|-----------|
| 1 | Item Zero (footgun guard) · O-1 · O-3 · Q-3 · Q-5 · Q-6 · 5.3 · 5.4 · 4.3 | All S/near-S, no prereqs — immediate latency, credibility, and product value |
| 2 | O-2 · O-4 · Q-1 · Q-2 · O-9 | Deepest perceived-latency win + local embedder + FE structural unblock |
| 3 | O-5 · O-8 · Q-4 · Q-7 · 2.2 · 6.7 | Hide remaining retrieval latency; scale/quality prerequisites; cheap features |
| 4 | O-6 · O-7 (post-legal-ruling) · 1.1 + 1.3 · 3.1-A | Streaming STT + in-region inference decision; unlock real calls + real handoff |
| 5 | 1.2 · 2.1 · 2.5 · 2.4 · 6.2 · O-10 | Scale outbound, close the follow-up loop, measure it |
| 6+ | 6.4 · 6.1 · 4.1 · 6.5 · 2.3 · 3.2 · 6.3 · 6.6 · 4.2 · 1.4 · 3.3 | Second-client readiness, inbound product, experimentation platform |

---

# Corrections & Stale-Docs Appendix

Findings surfaced during the research for this document, worth fixing alongside
whichever wave touches the same files:

- **`docs/DEPLOY.md`'s "FE is demo-only" note is stale.** It was written when
  the deployed frontend was built without `VITE_KOEL_API` and fell back to
  offline simulation. As of this session, the deployed FE has been verified
  live in a real browser session — login, dashboard, Knowledge Base, and a full
  voice call all reach the real backend. Update the note to reflect this (part
  of the docs refresh, not a new roadmap item).
- **render.yaml vs. actual deploy topology** — see **Q-6** above for the full
  fix; noted here because it's easy to trip over while reading other docs that
  assume the 5-service split is live.
- **"Bedrock" in FE copy** (`src/App.tsx:1656, 2123, 3037`) — NLP is Groq, not
  AWS Bedrock (Bedrock Titan is a stub, item **6.8**). Tracked in **Q-3**.
- **Keepalive workflow vs. starter-plan coherence** — `.github/workflows/keepalive.yml`
  exists to fight free-tier spin-down, but `render.yaml` declares `starter`
  plans (no spin-down) while the *actual* live deploy runs the free-tier
  `combined.py` single-process app. Resolve alongside Q-6's topology decision:
  either the keepalive workflow is load-bearing (free tier is real) and
  render.yaml should say so, or render.yaml is aspirational and the keepalive
  workflow can be retired once `starter` is actually applied.
- **`backend/.coverage`** — checked during this research pass and confirmed
  **git-ignored, not committed**; it's a local artifact, not a repo hygiene
  issue. No action needed.

---

# Critical Files Index

The files that nearly every item in this document touches, across all three parts:

- **`backend/koel/voicebot/pipeline/orchestrator.py`** — warm-up coordination (O-1), streaming sentence loop (O-2), speculative retrieval (O-5), per-call budgets (O-9), escalation hold (3.1, done), action dispatch (2.1/2.2), tone mapping (4.3), flow interpreter hook (6.6) — nearly every feature and optimization touches it.
- **`backend/koel/db/models.py`** — `BotConfig.config` seam, `CallLog`/`TranscriptTurn`, `org_id` columns; schema home for campaigns, experiments, consent, exports, per-bot overrides.
- **`backend/koel/nlp/providers.py`** / **`backend/koel/nlp/rag.py`** — NLP first-token streaming (O-2), local embedder (O-4), Sarvam NLP provider (O-7 Track A), `BedrockTitanEmbedder` (6.8, done), per-call cost recording (O-9).
- **`backend/koel/tts/providers.py`** — Sarvam language map (line 354), `RoutingTtsProvider` telephony/µ-law choke point (line 512), voice threading (4.2), `OrpheusSageMakerProvider` (6.8 — real boto3 mechanics, payload contract assumed/flagged pending a live endpoint).
- **`backend/koel/voicebot/clients.py`** — TTS cache wrapping (O-3), streaming clients (O-2/O-6), CRM adapter swap point (6.1).
- **`backend/koel/voicebot/ws/handler.py`** — reference transport for the Twilio bridge (1.1); `_finalize` hook where the summarizer, WhatsApp follow-up (2.4), and QA scoring (5.5) attach.
- **`backend/koel/crm/schemas.py`** — the frozen contract the live-CRM adapter protocol (6.1), PTP capture (2.1), and callback flags (2.5) all build on.
- **`src/App.tsx`** — decomposition targets (Q-1), dashboard/config truthful-UI wiring (Q-3), the FE half of nearly every Theme 3/6 feature until Q-1 splits it up.
