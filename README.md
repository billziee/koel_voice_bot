# KOEL — Voice Bot Platform for Financial Services

A low-latency, multilingual (English / Hindi / Hinglish) outbound voice-bot
platform built for Indian financial services — credit-card cross-sell,
pre-due collections, personal-loan and insurance calls — where every bot is
**configuration, not code**: the same engine serves every use case, persona,
and script from a database row and a Bot Builder wizard.

**Live demo:** frontend at `koel-voice-bot.onrender.com`, backend at
`koel-backend-oj5p.onrender.com` (login: `demo` / `koel-demo`).

---

## What's real here

Unlike a typical MVP scaffold, the AI pipeline in this repo is **fully live**,
not stubbed:

- **STT** — Groq Whisper (language-pinned) + Sarvam, real transcription
- **NLP** — Groq, language-routed (fast 8B model for English, 70B for
  Hindi/Hinglish), RAG-grounded via a real embedder
- **TTS** — three real engines routed by language: Groq Orpheus (English),
  NVIDIA Magpie (Hindi/Devanagari), Sarvam Bulbul (Hindi + Hinglish, with
  true WebSocket streaming)
- **RAG** — pgvector + a real NVIDIA embedder, 3 seeded knowledge bases, plus
  file-upload ingestion (drop in a PDF/text doc, it's chunked and embedded)
- **Conversation quality** — real dual-gated barge-in (interrupting the bot
  feels instant), a neural VAD in the browser, and a semantic end-of-turn
  gate so the bot doesn't answer a sentence you haven't finished

Everything else you'd expect from a production-shaped system is also there:
JWT auth on every REST call and the WebSocket upgrade, session recovery
across a dropped connection, RBI/IRDAI/DPDP/TRAI compliance logic, OpenTelemetry
tracing, and 264 tests.

**What's honestly not there yet**: a literal sub-300ms round-trip (physically
impossible with the caller in India and inference in the US — see
[`docs/SLO.md`](docs/SLO.md)), full India data residency, and real telephony
(it's a browser-based demo, not yet bridged to a phone carrier). See
[`docs/FUTURE_PROCEEDINGS.md`](docs/FUTURE_PROCEEDINGS.md) for the complete,
prioritized plan to close every open gap.

---

## Architecture, in one paragraph

A React/Vite frontend talks to a FastAPI backend over an authenticated
WebSocket (audio in, transcript + audio out). The backend is architecturally
five microservices — orchestrator (VoiceBot_svc), STT, NLP, TTS, and a dummy
CRM — but in the live deployment they run as **one Python process**
(`koel.combined:app`) to fit a memory-constrained free-tier host. Session
state lives in Redis with a TTL; calls, transcripts, bot configs, and RAG
knowledge live in Postgres + pgvector. See [`docs/HLD_V2.md`](docs/HLD_V2.md)
for the full system design and [`docs/LLD_V2.md`](docs/LLD_V2.md) for
code-level detail.

---

## Quickstart

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Full stack via Docker (Postgres+pgvector, Redis, all 5 services)
docker compose up --build
docker compose exec voicebot python -m scripts.bootstrap_db   # seed once

# Or run the tests without any external services:
pytest -q
ruff check koel tests scripts
```

Real AI providers activate automatically once their API keys are set (see
[`backend/README.md`](backend/README.md) for the full flag/config reference);
without keys, every service falls back to a deterministic stub so the whole
pipeline still runs end-to-end offline.

### Frontend

```bash
npm install
npm run dev          # http://localhost:5173, talks to localhost:8100 by default
npm run build         # tsc + vite build
npm run test:e2e       # Playwright
```

Set `VITE_KOEL_API` (build-time) to point the frontend at a non-default
backend origin.

---

## Documentation index

| Doc | What it's for |
|---|---|
| [`docs/PRD_V2.md`](docs/PRD_V2.md) | Current conformance status against the original PRD, section by section |
| [`docs/HLD_V2.md`](docs/HLD_V2.md) | System architecture (current) |
| [`docs/LLD_V2.md`](docs/LLD_V2.md) | Code-level design (current) |
| [`docs/FUTURE_PROCEEDINGS.md`](docs/FUTURE_PROCEEDINGS.md) | Every planned feature, optimization, and code advancement — prioritized, with exact files and approach |
| [`docs/PRODUCTION_MASTERPLAN.md`](docs/PRODUCTION_MASTERPLAN.md) | The path from "hardened code" to a real operated, compliant, deployed service (infra, secrets, compliance sign-off, telecom onboarding) |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | How the live Render deployment is actually configured |
| [`docs/SLO.md`](docs/SLO.md) | Measured latency numbers and the renegotiated (physically achievable) latency targets |
| [`docs/RESIDENCY.md`](docs/RESIDENCY.md) | The data-residency gap (US-hosted inference vs. India-residency requirement) and mitigation options |
| [`docs/TELEPHONY.md`](docs/TELEPHONY.md) | What a real PSTN (Twilio/Exotel) bridge needs, on top of the transcoding seam that already exists |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Security posture and secret-handling rules |
| [`backend/README.md`](backend/README.md) | Backend run guide, service/provider matrix, feature flags |
| [`docs/HLD.md`](docs/HLD.md) / [`docs/LLD.md`](docs/LLD.md) | **Superseded** — the original MVP-boilerplate design docs, kept for history |

---

## Tech stack

Python 3.11 + FastAPI (async, native WebSocket) on the backend; React 18 +
Vite + Tailwind on the frontend; PostgreSQL 16 + pgvector for data and RAG;
Redis for session state; Groq, Sarvam, and NVIDIA for live inference;
OpenTelemetry for tracing. Deployed on Render.
