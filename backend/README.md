# KOEL — Backend

FastAPI monorepo implementing the KOEL Voice Bot backend. See
[`../docs/PRD_V2.md`](../docs/PRD_V2.md) for the current conformance status
against the original PRD (v1.2), and [`../docs/LLD_V2.md`](../docs/LLD_V2.md)
for the full code-level design. This file is a quick orientation + run guide.

## Services

| Service        | Module                  | Port | Role |
|----------------|-------------------------|------|------|
| VoiceBot_svc   | `koel.voicebot.main`    | 8100 | Orchestrator: session lifecycle, STT→NLP→TTS routing, WebSocket, barge-in, filler, smart-turn endpointing, recovery |
| STT_svc        | `koel.stt.main`         | 8101 | Speech-to-text — **real**: Groq Whisper (language-pinned) + Sarvam; stub for offline/CI |
| NLP_svc        | `koel.nlp.main`         | 8102 | Intent + RAG-grounded response + tone tag — **real**: Groq, language-routed (8B for English, 70B for Hindi/Hinglish); stub for offline/CI |
| TTS_svc        | `koel.tts.main`         | 8103 | Tone-aware TTS — **real, 3 engines**: Groq Orpheus (English), NVIDIA Magpie (Hindi/Devanagari), Sarvam Bulbul (Hindi+Hinglish, WebSocket streaming); stub for offline/CI |
| Dummy CRM      | `koel.crm.main`         | 8104 | Synthetic customer records behind the production CRM contract |

All four services ship with **deterministic stub providers** (used
automatically when no API key is configured, and in tests/CI), so the entire
pipeline runs locally without any external accounts. With keys configured, the
real providers above are what actually runs — see the flags table below.
Provider selection is a config-only switch; no orchestrator changes are needed
to add, remove, or swap a vendor (proven in practice: Sarvam was added as a
second real STT+TTS vendor after Groq with zero orchestrator changes).

## Key feature flags (`KOEL_` env)

| Flag | Default | Effect |
|---|---|---|
| `SARVAM_TTS_WS_ENABLED` | `true` | WebSocket streaming TTS for Hindi/Hinglish (Sarvam); auto-falls-back to HTTP on any failure |
| `SMART_TURN_ENABLED` | `true` | Semantic end-of-turn gate (ONNX); self-disables if the model/onnxruntime is unavailable |
| `STREAM_TTS` | `true` | Sentence-streaming TTS — first sentence plays before the rest of the reply is generated |
| `ENFORCE_CALLING_HOURS` | `false` | RBI fair-practice calling window, evaluated in `CALLING_HOURS_TZ` (default `Asia/Kolkata`) |
| `ESCALATE_ON_UNSUPPORTED_LANGUAGE` | `true` | Graceful fallback + human escalation for a genuinely unsupported language |
| `TELEPHONY_MODE` | `false` | Transcode synthesized audio to 8kHz µ-law for a PSTN leg (no carrier bridge consumes this yet) |
| `KEEPALIVE_URL` | unset | Self-ping target to prevent free-tier idle spin-down (see `docs/DEPLOY.md`) |

See [`LLD_V2.md`](../docs/LLD_V2.md) §9 for the full configuration reference.

## Engineering standards baked in (PRD §08)

Every service inherits these from `koel.common.create_app`:
- **OpenAPI/Swagger** at `/docs`, `/redoc`, `/openapi.json` (Richardson L2).
- **OpenTelemetry** spans across FE → VB_Svc → AI services; per-hop latency.
- **Structured logging** with severity discipline + per-request correlation id.
- **Shared error envelope** with disciplined HTTP codes (400/422/500).
- **JWT auth** on every REST endpoint and on the WebSocket upgrade (1008 reject).
- **Redis session state** with TTL; no in-memory state in app servers.

## Conversation resilience (PRD §05) — implemented

- **Dual-gated barge-in** — `koel.voicebot.pipeline.bargein` (Gate 2 + `TTS_KILL`).
- **Latency-masking filler** — `koel.voicebot.pipeline.filler` + orchestrator race.
- **Silence / timeout rules** — `koel.voicebot.pipeline.silence` (5s re-prompt, +5s close).
- **15s WebSocket recovery** — grace TTL in `koel.voicebot.session.store`.
- **Graceful language fallback** + escalation — orchestrator.
- **Tone-tagged prosody** — `koel.tts.schemas.TONE_PROSODY`.

## Run it

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Option A — full stack via Docker (Postgres+pgvector, Redis, all 5 services)
docker compose up --build
# then seed the DB:
docker compose exec voicebot python -m scripts.bootstrap_db

# Option B — local: bring up Postgres+pgvector and Redis yourself, then
cp .env.example .env
python -m scripts.bootstrap_db
uvicorn koel.crm.main:app --port 8104 &
uvicorn koel.stt.main:app --port 8101 &
uvicorn koel.nlp.main:app --port 8102 &
uvicorn koel.tts.main:app --port 8103 &
uvicorn koel.voicebot.main:app --port 8100
```

### Demo flow

1. `POST /v1/auth/login` `{"username":"demo","password":"koel-demo"}` → access token.
2. `GET /v1/bots` → pick a bot (`bot_hdfc_cc`, `bot_icici_col`, `bot_insurance`).
3. `POST /v1/sessions` `{"bot_id":"bot_hdfc_cc","customer_id":"cust_cc_001"}` →
   `session_id` + `ws_token`.
4. Connect WebSocket to `ws://localhost:8100/ws/calls/{session_id}?token={ws_token}`.
5. Send `audio_chunk` frames (`is_final:true` ends an utterance); receive
   `transcript`, `audio_chunk`, `call_state`, and `call_ended` frames.

### Drive a full live call (headless)

With the stack up and seeded, `scripts/demo_call.py` runs a complete call over
the WebSocket — login → session → greeting → multi-turn conversation → barge-in
→ escalation — printing every frame:

```bash
python -m scripts.demo_call
```

## React frontend (live wiring)

The dashboard in `../src` is wired to this backend (`src/lib/koel.ts`):

- **Login** → `POST /v1/auth/login` (seeded `demo` / `koel-demo`). If the backend
  is unreachable it falls back to an offline simulation so the showcase still runs.
- **Dashboard** loads real instances from `GET /v1/bots`.
- **Active Call** opens the authenticated WebSocket: it `POST`s `/v1/sessions`,
  connects `ws://…/ws/calls/{id}?token=…`, and renders `transcript`, `call_state`,
  `filler`, `tts_kill`, and `call_ended` frames live. Chips send customer
  utterances (`audio_chunk` with the `TEXT:` type-to-talk payload); the barge-in,
  unsupported-language, language-switch, and network-drop controls exercise the
  real resilience paths against VB_Svc.

```bash
cd .. && npm install && npm run dev   # http://localhost:5173
# Set VITE_KOEL_API to point at a non-default backend origin.
```

CORS is enabled in the shared app factory (`KOEL_CORS_ALLOW_ORIGINS`) so the
dev-server origin can call the API.

## Tests

264 tests (ruff-clean) cover the resilience engines, JWT, prosody, PII masking,
the service-principal authZ guard, every real provider's request shape and
fallback-on-failure behavior, language pinning + script correctness, the
smart-turn gate (including graceful self-disable without the model file), and
config-default smoke tests:

```bash
pytest -q
ruff check koel tests scripts
```

## Audit hardening (applied)

An end-to-end audit drove these fixes:

- **No silent call loss** — a mid-turn AI-service error no longer crashes the
  socket; it surfaces an `error` frame and finalises the call (`ws/handler.py`).
- **Exactly-once persistence** — `_finalize` is idempotent and only flushes
  Redis *after* the call is safely in PostgreSQL (retries on DB failure).
- **AuthZ, not just authN** — sessions are bound to `owner_sub`; the WebSocket
  upgrade rejects a token whose `sub` doesn't own the session (no eavesdropping).
  The STT/NLP/TTS/CRM services now require a **service principal** (`role=service`),
  so a leaked operator token can't drive inference directly (cost abuse).
- **Consent + disclosure** — `greet()` renders the bot's greeting template,
  guarantees the automated-call disclosure, and sets `consent_captured` (§11/§12).
- **Prod secret fail-fast** — boot refuses the default JWT secret in production.
- **PII masking helper** (`mask_name`/`mask_account`) applied to identifier logs (§14).
- **Frontend** — the live WebSocket is closed and the call finalised on
  unmount / "Exit Sandbox"; `LiveCall.close()` detaches handlers; malformed
  frames and socket errors are handled; the config panel now persists new bots
  to VB_Svc (`POST /v1/bots`).

## Known limitations (current)

See [`../docs/FUTURE_PROCEEDINGS.md`](../docs/FUTURE_PROCEEDINGS.md) for the
full, prioritized list. Highlights:

- **Data residency**: inference runs on US-hosted Groq/NVIDIA; only Sarvam
  (part of the STT/TTS pipeline) is India-hosted. See
  [`../docs/RESIDENCY.md`](../docs/RESIDENCY.md).
- **Two provider stubs remain unimplemented seams**: `WhisperSageMakerProvider`
  and `OrpheusSageMakerProvider` (AWS-residency path). A third,
  `BedrockTitanEmbedder`, is *selectable* in config but raises at first use
  rather than at boot — a startup-validation guard for this is the top item in
  the roadmap.
- **No first-token NLP streaming or streaming STT yet** — both providers are
  still request/response, not incrementally streamed to/from the orchestrator
  (TTS *is* sentence-streamed and, for Sarvam, WebSocket-streamed).
- **Human escalation ends the call rather than transferring it** — the full
  handoff payload (transcript + CRM context) is built and sent; there's no
  agent queue or console to receive it yet.

## Layout

```
koel/
  common/     shared boilerplate: config, logging, telemetry, auth, errors, app
              factory, http (shared client factory), ratelimit, resilience
              (circuit breaker), text_guard (PII redaction)
  db/         SQLAlchemy models + schema (sessions, calls, transcripts, CRM, pgvector RAG)
  stt/        STT_svc — stub, Groq Whisper, Sarvam, SageMaker seam
  nlp/        NLP_svc (+ RAG retrieval) — stub, Groq (language-routed); embedders:
              hash stub, real NVIDIA, Bedrock Titan seam
  tts/        TTS_svc — stub, Groq Orpheus, NVIDIA Magpie, Sarvam (HTTP+WS),
              routing/fallback cascade, SageMaker seam
  crm/        Dummy CRM (+ seed data)
  voicebot/   VoiceBot_svc orchestrator
    session/  state, Redis store, lifecycle
    pipeline/ orchestrator, bargein, filler, silence, endpointing (smart-turn),
              localization, transliteration, summarizer, whisper_features
    ws/       protocol (incl. playback_done), handler
    api/      auth (+ refresh), bots (+ PATCH), sessions, history, analytics,
              knowledge (KB admin + file-upload ingestion)
scripts/      bootstrap_db.py, loadtest.py, fetch_smart_turn_model.sh
tests/        264 tests
```

See [`../docs/FUTURE_PROCEEDINGS.md`](../docs/FUTURE_PROCEEDINGS.md) for what's
planned next, and [`../docs/HLD_V2.md`](../docs/HLD_V2.md) /
[`../docs/LLD_V2.md`](../docs/LLD_V2.md) for the full architecture writeup.
