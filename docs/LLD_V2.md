# KOEL Voice Bot — Low-Level Design v2

> Code-level design for the **current, live** system. Supersedes [`LLD.md`](./LLD.md)
> (v1 — kept for historical reference). Companion: [`HLD_V2.md`](./HLD_V2.md).
> Paths are relative to `backend/` unless noted. Line references are indicative.

---

## What changed since v1 — module-level diff

| Area | v1 | v2 |
|---|---|---|
| **New pipeline modules** | `bargein.py`, `filler.py`, `silence.py`, `orchestrator.py` only | + `endpointing.py` (smart-turn v3.2 semantic gate), `localization.py` (single-source hi/hinglish operational copy), `transliteration.py` (Sarvam Devanagari→romanized, startup-only), `summarizer.py` (post-call Sarvam-chat CRM summary), `whisper_features.py` (vendored numpy log-mel features for smart-turn) |
| **New voicebot files** | — | `recording.py` (S3 call-recording upload, no-op when unconfigured) |
| **New REST routers** | `auth.py` (login only), `bots.py`, `sessions.py`, `history.py` | + `POST /v1/auth/refresh`; + `bots.py` gained `PATCH /{bot_id}`; + `knowledge.py` (KB admin: list/create/delete chunks, **file-upload ingestion**); + `analytics.py` (per-bot + org-wide stats) |
| **New common modules** | `app_factory, auth, config, logging, telemetry, errors, schemas` | + `http.py` (shared `make_async_client` — HTTP/2, long keep-alive, used by every AI provider), `ratelimit.py` (in-memory, single-worker), `resilience.py` (circuit breaker + retry/backoff), `text_guard.py` (PII redaction before TTS/logs) |
| **STT providers** | `StubWhisperProvider` only (real = `NotImplementedError` seam) | `StubWhisperProvider`, **`GroqWhisperProvider`** (real, language-**forced**), **`SarvamSttProvider`** (real), `WhisperSageMakerProvider` (still a stub seam) |
| **TTS providers** | `StubOrpheusProvider` only | `StubOrpheusProvider`, **`GroqOrpheusProvider`** (real, English), **`NvidiaMagpieTtsProvider`** (real, Hindi/Devanagari), **`SarvamTtsProvider`** (real, Hindi+Hinglish, **HTTP and WebSocket streaming paths**), **`RoutingTtsProvider`** (language-based dispatch + automatic fallback cascade), `OrpheusSageMakerProvider` (still a stub seam) |
| **NLP providers** | `StubNlpProvider` only | `StubNlpProvider`, **`GroqNlpProvider`** (real, **language-routed model**: 8B for en, 70B for hi/hinglish) |
| **RAG embedder** | `HashEmbedder` (deterministic stub) only | `HashEmbedder`, **real NVIDIA embedder** (`nv-embedqa-e5-v5`, 1024-d), `BedrockTitanEmbedder` (stub seam — selectable but raises at first use, not at boot; flagged as a footgun in the roadmap) |
| **WS protocol** | `audio_chunk, barge_in_candidate, pong, end_call` (client); 8 server types | + client `playback_done` (audio queue drained — anchors the no-input clock to real playback end, not a send-time estimate) |
| **Session state** | `no_input_strikes` only | + language-request tracking for explicit mid-call switches; ambient profile is now toggle-driven from bot config |
| **Config surface** | ~20 settings | + `sarvam_*` (STT/TTS/NLP-side transliteration+summary), `smart_turn_*`, `unsupported_confidence_floor` / `unsupported_min_speech_ms` (fallback-under-pinning), `calling_hours_tz`, `enforce_calling_hours` (now timezone-correct), `stream_tts`, keep-alive settings |
| **Tests** | 37, `backend/tests/` | **264**, same directory — new files for every module above |

---

## 1. Module layout (current)

```
backend/koel/
├── common/                    # shared infrastructure (inherited by every service)
│   ├── app_factory.py         # create_app(): docs, OTel, CORS, correlation+trace mw, /health
│   ├── auth.py                # JWT issue/decode, REST dep, WS auth, service principal
│   ├── config.py              # CommonSettings (KOEL_-prefixed, .env)
│   ├── http.py                # make_async_client() — HTTP/2 + long keep-alive, shared by all AI providers
│   ├── logging.py             # JSON/console formatters, correlation ContextVar, PII masks
│   ├── ratelimit.py           # in-memory rate limiter (single-worker; Redis-backed is roadmap O-8)
│   ├── resilience.py          # circuit breaker + retry/backoff for AI provider calls
│   ├── telemetry.py           # OTel provider, span() ctx-mgr, current_trace_id(), usage/cost counters
│   ├── text_guard.py          # PII redaction (account numbers, phone) before TTS/logs
│   └── schemas.py             # HealthResponse, Language, Tone enums
├── db/
│   ├── models.py               # SQLAlchemy: CrmCustomer, BotConfig, CallLog, TranscriptTurn, KnowledgeChunk
│   └── session.py              # async engine, sessionmaker, session_scope()
├── voicebot/                   # VB_Svc — the orchestrator (port 8100)
│   ├── main.py                 # app, lifespan (runtime + filler warm + keep-alive), WS endpoint
│   ├── config.py                # VoiceBotSettings (resilience thresholds, smart-turn, Sarvam, calling-hours tz)
│   ├── deps.py                  # Runtime dataclass + build_runtime + get_runtime (constructs SmartTurnGate when enabled)
│   ├── clients.py                # ServiceClients (httpx, svc-JWT, trace inject, streaming client)
│   ├── persistence.py            # persist_call() → Postgres
│   ├── recording.py               # S3 call-recording upload (no-op when bucket unconfigured)
│   ├── api/
│   │   ├── auth.py                # login + refresh + ws-token
│   │   ├── bots.py                 # list/create/get/PATCH (+ audit row on change)
│   │   ├── sessions.py              # start_session (DND 409, RBI-hours 403, DPDP-minimised CRM context)
│   │   ├── history.py               # call list + detail (+ transcript)
│   │   ├── analytics.py              # per-bot + org-wide stats (grouped queries, org-scoped)
│   │   └── knowledge.py               # KB chunk CRUD + file-upload ingestion (PDF/text → chunk → embed)
│   ├── ws/                             # protocol.py (message envelopes incl. playback_done), handler.py (CallSession)
│   ├── session/                         # models.py (SessionState/Turn/CallStatus), store.py (Redis)
│   └── pipeline/
│       ├── orchestrator.py                # turn state machine (unchanged shape, new language/script logic)
│       ├── bargein.py                      # dual-gate + min-words gate
│       ├── endpointing.py                   # smart-turn v3.2 semantic end-of-turn gate (self-disabling)
│       ├── filler.py                         # latency-masking clip cache
│       ├── silence.py                         # no-input rules, language-correct script per bot
│       ├── localization.py                     # single-source hi/hinglish operational copy
│       ├── transliteration.py                    # Sarvam Devanagari→romanized (startup only)
│       ├── summarizer.py                          # post-call Sarvam-chat → structured CRM summary
│       └── whisper_features.py                     # vendored numpy log-mel features (smart-turn dependency)
├── stt/                        # Groq Whisper + Sarvam + stub + SageMaker seam; language-forced transcription
├── nlp/                        # Groq (language-routed 8B/70B) + stub; real NVIDIA/stub/Bedrock-seam embedder + RAG
├── tts/                        # Groq Orpheus + NVIDIA Magpie + Sarvam (HTTP+WS) + routing + stub + SageMaker seam
└── crm/                        # Dummy CRM (main + schemas + seed) — now also accepts POST .../call-summary
```

---

## 2. Shared core (`koel/common`) — additions since v1

Sections 2.1–2.5 (app_factory, auth, logging, telemetry, errors) are **unchanged
from v1** — see [`LLD.md`](./LLD.md) §2 for full detail. New modules:

### 2.6 `http.py`
`make_async_client(*, timeout_s, http2=True)` — every AI provider client (Groq,
Sarvam, NVIDIA) is built from this single factory: HTTP/2 enabled, a long
keep-alive window (holds pooled connections well past a typical inter-turn
think-time), so the TLS/HTTP2 handshake to a US-hosted provider is paid once per
session rather than once per turn. Roadmap **O-1** extends this with a
session-start warm-up call so the *first* turn isn't the one paying that cost.

### 2.7 `ratelimit.py`
An in-memory `RateLimiter` used on `/v1/auth/login` and `/v1/auth/refresh`.
**Known limitation** (tracked as roadmap **O-8**): state lives in one worker's
process memory, so limits are wrong the moment there's more than one worker or
instance — a Redis-backed implementation is the fix, not yet built.

### 2.8 `resilience.py`
A circuit breaker (closed/open/half-open) + `retry_async` with backoff/jitter,
wrapping outbound AI-provider calls so a flaky upstream degrades to the stub
fallback rather than cascading failures into the socket.

### 2.9 `text_guard.py`
`redact_pii(text)` — masks account/card/phone-length digit runs before text is
spoken by TTS or written to logs, while leaving names and short figures (fees,
reward points) intact. Called on every bot turn in the orchestrator.

---

## 3. Data model (`koel/db/models.py`)

**Unchanged shape from v1** — `CrmCustomer`, `BotConfig`, `CallLog`,
`TranscriptTurn`, `KnowledgeChunk` and their relationships are the same; see
[`LLD.md`](./LLD.md) §3 for the full ER diagram and field list. Notable v2
facts:

- `KnowledgeChunk.embedding` is genuinely populated now — 3 seeded indexes
  (`cc_hdfc` credit-card, `col_icici` collections, `ins_term` insurance) plus
  whatever's been added via the file-upload endpoint, all embedded with the
  real NVIDIA embedder (1024-d, matching `EMBEDDING_DIM`).
- `BotConfig.config` (free-form JSON) remains the reserved seam for CRM field
  mapping, backchannel dictionaries, and escalation rules — the roadmap's
  Part C features (campaigns, experiments, flow editor, per-bot LLM overrides)
  are all designed to extend this column rather than add new top-level tables
  where a JSON field will do.
- `org_id` (nullable) is present on `BotConfig`, `CallLog`, `KnowledgeChunk` and
  is consistently applied wherever it's set, but is not yet enforced
  non-null — see roadmap **6.4**.

---

## 4. WebSocket protocol (`voicebot/ws/protocol.py`) — one addition

All v1 message types are unchanged. One new **client→server** message:

| type | payload | meaning |
|---|---|---|
| `playback_done` | — | the client's local audio queue has fully drained — the bot has *actually* finished speaking. Does **not** count as customer input (doesn't reset the no-input timer's "customer spoke" state) but **does** anchor the no-input clock's start time to real playback end instead of a server-side synthesis-duration estimate, which drifted once sentence-streaming and WS-streamed TTS made "how long will this audio play" harder to predict ahead of time. |

See [`HLD_V2.md`](./HLD_V2.md) §6.2 for why this was needed (streaming TTS made
the old estimate unreliable).

---

## 5. Session state & store — unchanged

`SessionState`, `Turn`, `CallStatus`, and `RedisSessionStore` are **unchanged
from v1** — see [`LLD.md`](./LLD.md) §5. The ambient-toggle and ambient-profile
handling now reads a per-bot config value (`silent`/`off`/`none` → no bed;
anything else, default `callfloor` → mixed in) rather than always defaulting on.

---

## 6. The orchestrator (`voicebot/pipeline/orchestrator.py`) — behavior changes

The v1 turn state machine (greet → listen → process → respond → listen/escalate/end)
is **unchanged in shape** — see [`LLD.md`](./LLD.md) §6 for the flowchart, which
still applies. What changed inside individual steps:

- **`greet()`** now selects greeting/disclosure/consent-ask copy **per language**
  from `pipeline/localization.py`'s tables (English literal, Hindi Devanagari,
  Hinglish romanized) instead of a single hardcoded Hinglish default — an
  English bot with no custom greeting template used to speak Hinglish; it now
  speaks English.
- **`handle_final_transcript()`**'s language-fallback check
  (`_is_unsupported_language`) gained a **second trigger**: alongside the v1
  behavior (STT reports a non-supported `language_id` above a confidence
  threshold), it now also fires when a **sustained, real utterance
  force-transcribes into the bot's pinned language with very low confidence** —
  necessary because STT no longer auto-detects language (see §8.1 below), so
  the original trigger alone can never fire under pinning.
- **Mid-call language switching** is now **explicit-request-only**: a caller
  saying "continue in English" / "Hindi mein baat karo" locks the language
  (a small regex+keyword detector, `_requested_language()`); the old behavior
  of trusting STT's per-utterance `language_id` guess is disabled by default
  (`adapt_language_to_caller: bool = False`) because it was unreliable on
  code-mixed audio and caused mid-call flip-flopping.
- **`_send_bot_turn()`**'s ambient mixing now gates on the bot's
  `ambient_profile` value (see §5) in addition to the caller-supplied
  `ambient` flag — a bot with ambient toggled off in Configuration/Bot Builder
  never gets the floor-chatter bed, even on turns where the caller didn't
  explicitly ask for silence.
- **Script policy is now explicit and tested**: every hardcoded operational
  string the orchestrator can speak (greeting, disclosure, consent-ask,
  no-input reprompt/close, filler phrases) is authored in the correct script
  for its TTS engine — Devanagari for Hindi (Magpie/Sarvam), romanized Latin
  for Hinglish (Sarvam/Orpheus) — closing a class of bug where romanized text
  reached a Devanagari-only engine and came out as gibberish.

### 6.1 New pipeline sub-modules

- **`endpointing.SmartTurnGate`** — a lazy, self-disabling wrapper around the
  smart-turn-v3.2 ONNX model (vendored from `pipecat-ai/pipecat`). Given the
  tail of an utterance's audio, scores whether the speaker sounds mid-thought
  vs. genuinely done; if mid-thought, the handler holds the transcript up to
  `smart_turn_hold_ms` and merges the next utterance instead of answering a
  half-formed sentence. Inference runs in a worker thread (~50–200ms CPU) so it
  never stalls the event loop. If `onnxruntime` or the model file is missing,
  it silently disables itself on first use rather than raising — safe to leave
  on by default in any environment, including ones without the model shipped.
- **`localization.py`** — the single canonical source for hi/hinglish
  operational copy. Devanagari text is hand-authored; the Hinglish (romanized)
  form is **derived from it via Sarvam transliteration at startup**
  (`warm()`), not hand-maintained separately — this was a deliberate fix for a
  class of bug where the two forms drifted and romanized text ended up
  reaching the Devanagari-only Magpie engine. A hardcoded Hinglish fallback
  ships with every entry so a Sarvam outage at boot only means transliteration
  didn't refresh, not that the copy is missing.
- **`transliteration.py`** — the Sarvam client used only by `localization.warm()`
  at startup; never on the live-call path, so a Sarvam outage here affects
  boot-time copy generation only, not call latency.
- **`summarizer.SarvamSummarizer`** — post-call only (invoked from
  `ws/handler.py`'s `_finalize`), extracts structured insight
  (interest/objection/sentiment/next_action) from the full transcript via
  Sarvam chat completions and posts it to the CRM (`POST .../call-summary`).
  Entirely off the in-call critical path.
- **`whisper_features.py`** — vendored unmodified from `pipecat-ai/pipecat`
  (BSD-2-Clause): numpy-only Whisper-style log-mel feature extraction, a
  dependency of the smart-turn v3.2 model's audio preprocessing.

---

## 7. WebSocket handler — `CallSession` (`voicebot/ws/handler.py`)

**Unchanged in structure from v1** — `run()`, `_receive_loop()`,
`_handle_barge_in()`, `_handle_no_input()`, `_heartbeat_loop()`,
`_on_disconnect()`, `_await_grace_then_finalize()`, `_finalize()` all work as
described in [`LLD.md`](./LLD.md) §7, including the pong-independent no-input
deadline and the exactly-once dropped-call finalizer. What changed:

- **`_receive_loop()`** now also handles the new `playback_done` client
  message: it updates `_speech_until` to "now" (the bot has genuinely stopped)
  without treating the message as customer input, so the no-input clock starts
  from real playback end rather than a synthesis-duration estimate.
- **`_handle_barge_in()`**'s classifier call now also applies the min-words
  gate from `bargein.py` (see §6.1 of [`HLD_V2.md`](./HLD_V2.md)) — a single
  junk word transcribed from STT noise no longer triggers `tts_kill`.
- **Recording**: on finalize, if an S3 bucket is configured
  (`voicebot/recording.py`), the accumulated call audio is uploaded and
  `CallLog.recording_url` is populated; unconfigured, this is a no-op (not an
  error) — the field stays `None`.

---

## 8. AI microservices — provider inventory (the biggest change since v1)

### 8.1 STT (`koel/stt`)

`TranscribeRequest`/`TranscriptResult` schemas are unchanged. Provider classes:

| Class | State | Notes |
|---|---|---|
| `StubWhisperProvider` | stub | Type-to-talk (`b"TEXT:"` prefix) + heuristic language markers for offline/CI use |
| `GroqWhisperProvider` | **real** | `whisper-large-v3-turbo`. **Language is forced** via the `language` parameter (`en`, `hi`, or `hi` for hinglish — Whisper has no hinglish code) rather than left to auto-detect. This was a deliberate fix: auto-detect on short, idle, or code-mixed Indian phone audio produced wrong-language guesses (observed: Hindi speech misidentified as Russian/Ukrainian; silence/noise misidentified as Spanish/French), which then triggered spurious behavior downstream. |
| `SarvamSttProvider` | **real** | `saaras` model, India-hosted. |
| `WhisperSageMakerProvider` | stub seam | `NotImplementedError`; unimplemented AWS-residency path (roadmap **6.8** / **O-7** Track B) |

### 8.2 NLP (`koel/nlp`)

Schemas (`TurnContext`, `NlpResponse`, `Intent` enum) are **unchanged from
v1**. Provider classes:

| Class | State | Notes |
|---|---|---|
| `StubNlpProvider` | stub | Keyword heuristics, templated responses (offline/CI use) |
| `GroqNlpProvider` | **real** | **Language-routed model selection**: `llama-3.1-8b-instant` for English turns, `llama-3.3-70b-versatile` for Hindi/Hinglish (code-mixed input is harder, so it gets the larger model). JSON-mode contract, system prompt is **language-strict** (explicitly instructs the model to reply in the turn's specified language/script, not a fixed Hinglish persona — an earlier version of the prompt biased even English-configured bots toward Hinglish replies). `max_tokens` tuned down (160) since spoken turns are short. Any exception still falls back to `StubNlpProvider`, unchanged from v1. |

**RAG embedder** (`nlp/rag.py`):

| Class | State | Notes |
|---|---|---|
| `HashEmbedder` | stub | Deterministic, used in tests/offline |
| NVIDIA embedder | **real** | `nv-embedqa-e5-v5`, 1024-d, multilingual (handles Hindi/Hinglish retrieval correctly) |
| `BedrockTitanEmbedder` | stub seam, **flagged risk** | Selectable via config (`nlp_embedder="bedrock"`) but `embed()` raises `NotImplementedError` at first *use*, not at process boot — a startup-validation guard is roadmap **Item Zero** in `FUTURE_PROCEEDINGS.md`. |

### 8.3 TTS (`koel/tts`)

`SynthesizeRequest`/prosody mapping are unchanged in shape. Provider classes:

| Class | State | Notes |
|---|---|---|
| `StubOrpheusProvider` | stub | Sized silent PCM + deterministic ambient bed (offline/CI use) |
| `GroqOrpheusProvider` | **real** | English only |
| `NvidiaMagpieTtsProvider` | **real** | Hindi. **Devanagari-only** — romanized text produces gibberish through this engine (a bug found and fixed this cycle: the sample rate was also wrong, 44.1kHz vs the pipeline's canonical 24kHz, independently causing pitch-shifted gibberish before the script issue was even found). |
| `SarvamTtsProvider` | **real** | `bulbul:v3`, Hindi **and** Hinglish (unlike Magpie, Sarvam voices romanized Hinglish correctly — verified via a Sarvam-STT round-trip check). Has **both an HTTP path** (`_stream_by_sentence`, one request per sentence) **and a true WebSocket streaming path** (`/text-to-speech/ws` — audio chunks arrive while synthesis is still running, not after a full round-trip). WS is **on by default**; any WS-path failure (connection error, mid-stream error frame) **automatically falls back to the HTTP path** for the rest of that turn. |
| `RoutingTtsProvider` | **real** | Dispatches by request language: Hindi/Hinglish → Sarvam (falling back to Magpie for Hindi only, then to the base English engine) when Sarvam is enabled; otherwise Hindi routes straight to Magpie (never Hinglish, since Magpie can't safely voice it) and Hinglish falls through to the base engine. |
| `OrpheusSageMakerProvider` | stub seam | `NotImplementedError`; unimplemented AWS-residency path |

### 8.4 CRM (`koel/crm`)

**Unchanged from v1** (`GET /v1/customers`, `GET /{id}`,
`POST /{id}/interactions`, `POST /{id}/flags`), **plus one new endpoint**:
`POST /{id}/call-summary` — receives the structured output of
`summarizer.SarvamSummarizer` and appends it to the customer's interaction log.

---

## 9. Configuration reference (`KOEL_` env) — additions since v1

The v1 settings (JWT, Redis/DB URLs, service URLs, latency budget, barge-in,
filler, no-input, session/heartbeat, `LANGUAGE_CONFIDENCE_THRESHOLD`) are
**unchanged** — see [`LLD.md`](./LLD.md) §9. New in v2:

| Setting | Default | Purpose |
|---|---|---|
| `SARVAM_API_KEY` / `SARVAM_BASE_URL` | — / `https://api.sarvam.ai` | shared across STT/TTS/transliteration/summarizer |
| `SARVAM_TTS_MODEL` / `SARVAM_SPEAKER` | `bulbul:v3` / `priya` | Sarvam TTS voice selection |
| `SARVAM_TTS_WS_ENABLED` | **`true`** | WebSocket streaming TTS; falls back to HTTP automatically on any failure — safe on by default |
| `SMART_TURN_ENABLED` | **`true`** | semantic end-of-turn gate; self-disables gracefully if the model/onnxruntime is unavailable — safe on by default |
| `SMART_TURN_MODEL_PATH` | `models/smart-turn-v3.2-cpu.onnx` | shipped in the Docker image via `scripts/fetch_smart_turn_model.sh` at build time |
| `SMART_TURN_THRESHOLD` / `SMART_TURN_HOLD_MS` | 0.5 / 1500 | gate sensitivity / max hold before answering anyway |
| `ADAPT_LANGUAGE_TO_CALLER` | `false` | STT-guess-driven mid-call language switching (off — unreliable on code-mixed audio; explicit-request switching is always on) |
| `ESCALATE_ON_UNSUPPORTED_LANGUAGE` | `true` | graceful fallback — safe now that STT is language-pinned (see §8.1, §6) |
| `UNSUPPORTED_CONFIDENCE_FLOOR` / `UNSUPPORTED_MIN_SPEECH_MS` | 0.35 / 800 | the fallback-under-pinning trigger: a sustained (≥800ms) real utterance transcribing below this confidence into the bot's pinned language is treated as an unsupported language |
| `ENFORCE_CALLING_HOURS` | `false` | RBI fair-practice window — opt-in, unchanged default |
| `CALLING_HOURS_TZ` | `Asia/Kolkata` | **new**: the window is now evaluated in this timezone, not the server's local/UTC clock — a bug fix (the window used to silently run in UTC on cloud hosts) |
| `STREAM_TTS` | `true` | sentence-streaming TTS (Tier 2) — one audio chunk per sentence instead of the whole clip |
| `TELEPHONY_MODE` | `false` | when true, synthesized audio is transcoded to 8kHz µ-law for a PSTN leg (still no carrier bridge consumes this — roadmap **1.1**) |
| keep-alive settings | — | self-ping interval/target for the free-tier Render deployment (voicebot service only) |

---

## 10. REST API reference — additions since v1

All v1 endpoints (`/v1/auth/login`, `/v1/auth/ws-token`, `/v1/bots` GET/POST,
`/v1/bots/{id}` GET, `/v1/sessions` POST, `/v1/calls` GET/GET-by-id, the
internal CRM contract, the WS upgrade, `/health`/`/docs`) are **unchanged** —
see [`LLD.md`](./LLD.md) §10. New in v2:

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/v1/auth/refresh` | none (rate-limited) | issues a fresh access token — **built but not yet consumed by the frontend**, see roadmap **Q-2** |
| PATCH | `/v1/bots/{bot_id}` | access | partial update; writes a `BotConfigAudit` row on any actual change |
| GET | `/v1/stats` · `/v1/bots/{bot_id}/stats` | access | org-wide / per-bot aggregated call stats |
| GET | `/v1/knowledge-indexes` | access | list RAG indexes + chunk counts |
| GET / POST / DELETE | `/v1/knowledge-chunks` | access | list (filterable) / create (with optional server-side chunking) / delete |
| POST | `/v1/knowledge-files` | access | **file-upload ingestion**: accepts a text or PDF file, extracts text (`pypdf` for PDF), splits into passages, embeds each, stores as chunks under a given `rag_index` — a 2MB size cap and non-empty-content validation happen before any DB write |

---

## 11. Token & trace flows — unchanged

See [`LLD.md`](./LLD.md) §11 — the REST/WS/service-to-service JWT and W3C
trace-propagation design is identical in v2.

---

## 12. Key algorithms — additions since v1

The seven v1 algorithms (dual-gate barge-in, filler racing, pong-independent
no-input, dropped-call finalizer, language fallback, tone→prosody, ambient
mixing) are **unchanged** — see [`LLD.md`](./LLD.md) §12. New:

8. **Min-words barge-in gate** — a confirmed interrupt additionally requires
   either ≥2 words or one word held ≥1200ms, so a single junk STT word (noise,
   a cough) doesn't kill TTS on its own. `bargein.py`.
9. **Smart-turn semantic hold** — an ONNX model scores "does this utterance
   sound complete", holding and merging on a mid-thought judgment rather than
   answering a fragment. Runs in a worker thread; self-disables on any failure.
   `pipeline/endpointing.py`.
10. **Language-pin-aware unsupported-language detection** — combines (a) a
    high-confidence non-supported STT language id (v1's original trigger,
    still live for non-pinned paths) with (b) a new signal: a sustained,
    force-transcribed utterance that comes back with very low confidence,
    which is what an actually-unsupported language looks like once STT can no
    longer auto-detect and flag it directly. `orchestrator._is_unsupported_language`.
11. **Devanagari/Latin script routing** — every hardcoded and templated
    operational string is authored in (or derived into, via startup
    transliteration) the script its assigned TTS engine can actually voice;
    tests assert this mapping holds for every language/engine pairing.
    `pipeline/localization.py` + orchestrator greeting logic.
12. **WS-streaming-with-HTTP-fallback** — the same cascade shape as the
    existing provider-fallback chain, applied to transport rather than vendor:
    Sarvam TTS tries its WebSocket path first; any connection or mid-stream
    error transparently degrades that turn to the HTTP sentence-by-sentence
    path. `tts/providers.SarvamTtsProvider`.

---

## 13. Testing strategy (`backend/tests`, 264 tests)

The v1 test categories (unit: bargein/silence/nlp/auth-prosody/audit/resilience;
integration: sessions-api/persistence/ws-finalizer; live drivers:
`scripts/demo_call.py`/`demo_resilience.py`) are **all still present and
green**. New test files cover every module in the diff table above: real
provider behavior (Groq/Sarvam/NVIDIA request shape, fallback-on-failure),
language pinning and script-correctness assertions, smart-turn gate behavior
(including graceful self-disable without the model file), the min-words
barge-in gate, calling-hours timezone correctness, knowledge-file upload
validation, and config-default smoke tests (e.g. asserting
`sarvam_tts_ws_enabled` and `smart_turn_enabled` default to `true`, since a
silent flip back to `False` would quietly regress latency without any other
test catching it).

**Gates:** `pytest -q`, `ruff check` — both clean as of this document.

---

## 14. Extension seams — updated

| To add | Where | Status |
|---|---|---|
| Real Whisper STT | `WhisperSageMakerProvider` or mirror `GroqNlpProvider`'s pattern | ✅ done via Groq + Sarvam; SageMaker path remains a seam |
| Real Orpheus TTS | `OrpheusSageMakerProvider.synthesize/stream` | ✅ done via Groq + Magpie + Sarvam; SageMaker path remains a seam |
| Real embeddings | replace `HashEmbedder` in `nlp/rag.py` | ✅ done via NVIDIA; Bedrock Titan remains a seam |
| First-token NLP streaming | new streaming route + incremental JSON scanner | not done — roadmap **O-2** |
| Streaming STT | new `stream_transcribe()` provider method | not done — roadmap **O-6** |
| Telephony (PSTN) | new transport calling the same `Orchestrator` (unchanged: only needs a `send` callback) | not done — the µ-law transcoding half exists (`tts/mixer.to_ulaw_8k`); no carrier bridge — roadmap **1.1** |
| New use case / bot | `POST /v1/bots` config row — no code | ✅ unchanged, still the product's moat |
| New language | add a `Language` enum member + walk its consumers | not done — roadmap **4.1** (Tamil/Telugu/Marathi/Bengali via Sarvam) |
| Local embedder | new `Embedder` subclass in `nlp/rag.py` | not done — roadmap **O-4** |
| Live CRM | adapter behind the existing 4-call contract | not done — roadmap **6.1** |

See [`FUTURE_PROCEEDINGS.md`](./FUTURE_PROCEEDINGS.md) for the full, prioritized
list of everything not yet done.
