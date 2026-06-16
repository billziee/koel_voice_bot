# KOEL — Backend (Phase 0 boilerplate + core services)

FastAPI monorepo implementing the KOEL Voice Bot backend per the PRD (v1.2).
This is the **Phase 0 / Sprint 0** deliverable: boilerplate that meets the §08
engineering standards plus the core microservices with swappable provider
interfaces.

## Services

| Service        | Module                  | Port | Role |
|----------------|-------------------------|------|------|
| VoiceBot_svc   | `koel.voicebot.main`    | 8100 | Orchestrator: session lifecycle, STT→NLP→TTS routing, WebSocket, barge-in, filler, recovery |
| STT_svc        | `koel.stt.main`         | 8101 | Speech-to-text (Whisper interface; stub provider) |
| NLP_svc        | `koel.nlp.main`         | 8102 | Intent + RAG-grounded response + tone tag (stub or live Groq inference) |
| TTS_svc        | `koel.tts.main`         | 8103 | Tone-aware TTS (Orpheus interface; stub provider) |
| Dummy CRM      | `koel.crm.main`         | 8104 | Synthetic customer records behind the production CRM contract |

The three AI services and the CRM ship with **runnable stub providers** so the
entire pipeline works locally without GPUs or AWS. Real Whisper/Bedrock/Orpheus
endpoints drop in behind the same provider interface — no orchestrator changes
(PRD §12 vendor-lock-in mitigation).

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

Pure-logic tests (no DB/Redis needed) cover the resilience engines, JWT,
prosody, PII masking, and the service-principal authZ guard:

```bash
pytest tests/
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

## Known MVP limitations (by design)

- **AI models are stub providers by default.** STT/NLP/TTS run deterministic
  stubs (silent PCM, keyword intent, hash embeddings) behind the real provider
  interfaces; Whisper/Orpheus/Titan drop in with no orchestrator change.
  **NLP can run live**: set `KOEL_NLP_PROVIDER=groq` + `KOEL_GROQ_API_KEY=...`
  to generate grounded turns via Groq (falls back to the stub on any error).
- **Ambient audio layer** (§06) is config-only — not mixed into the stub audio.
- **OTel cross-service stitching** — each service emits manual per-hop spans and
  a shared correlation id links logs, but W3C `traceparent` is not yet propagated
  FE→VB→AI into one trace (FastAPI auto-instrumentation is off; see config note).
- **Bot config update** — create persists to the backend; in-place *edit* of an
  existing bot is frontend-state only (no `PATCH /v1/bots/{id}` yet).

## Layout

```
koel/
  common/     shared boilerplate (config, logging, telemetry, auth, errors, app factory)
  db/         SQLAlchemy models + schema (sessions, calls, transcripts, CRM, pgvector RAG)
  stt/        STT_svc
  nlp/        NLP_svc (+ RAG retrieval)
  tts/        TTS_svc
  crm/        Dummy CRM (+ seed data)
  voicebot/   VoiceBot_svc orchestrator
    session/  state, Redis store, lifecycle
    pipeline/ bargein, filler, silence, orchestrator
    ws/       protocol, handler
    api/      auth, bots, sessions, history
scripts/      bootstrap_db.py
tests/        pure-logic unit tests
```
