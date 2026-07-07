# KOEL Voice Bot — Technical Handout

**Project**: KOEL Voice Bot Platform  
**Version**: 1.3.0 (Updated with Sarvam AI integration)  
**Last Updated**: 2026-07-07  
**Status**: MVP with Real Sarvam STT/TTS + Groq NLP + NVIDIA Magpie

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Technology Stack](#technology-stack)
3. [Data Models & Database Schema](#data-models--database-schema)
4. [REST API Reference](#rest-api-reference)
5. [WebSocket Protocol](#websocket-protocol)
6. [Core Components](#core-components)
7. [Configuration & Environment Variables](#configuration--environment-variables)
8. [Deployment Guide](#deployment-guide)
9. [Development Workflow](#development-workflow)
10. [Extension Points](#extension-points)

---

## System Architecture

### High-Level Overview

KOEL is a low-latency, multilingual voice-bot platform designed for Indian financial services. The system achieves **sub-300ms conversational turn latency** through persistent WebSockets, streaming AI services, and an orchestration-based pipeline.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Frontend (React SPA)                        │
│                  src/App.tsx (Vite + Tailwind)                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ REST + WebSocket (JWT)
                       ↓
┌──────────────────────────────────────────────────────────────────┐
│                  VoiceBot Orchestrator (8100)                    │
│  • Session lifecycle & WebSocket handler                         │
│  • Pipeline orchestration (STT → NLP → TTS)                      │
│  • Barge-in, filler injection, silence handling                  │
│  • Resilience & reconnection logic                               │
└──────────────┬─────────────┬─────────────┬──────────────────────┘
               │             │             │
        ┌──────↓───┐  ┌──────↓───┐  ┌─────↓────┐
        │ STT (8101)│  │NLP (8102)│  │TTS (8103)│
        └──────┬────┘  └──────┬────┘  └─────┬────┘
               │              │             │
               └──────────────┼─────────────┘
                              ↓
                    ┌─────────────────────┐
                    │   CRM Service (8104)│
                    │  (Synthetic demo)   │
                    └────────┬────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ↓                    ↓                    ↓
    ┌────────┐          ┌────────┐          ┌────────┐
    │ Redis  │          │ Postgres+ │      │ Groq   │
    │(session│          │ pgvector  │      │ API    │
    │ state) │          │(data)    │      │(live)  │
    └────────┘          └──────────┘      └────────┘
```

### Key Design Principles

- **Stateless Services**: All call state in Redis with TTL → horizontal scaling
- **Provider Isolation**: STT/NLP/TTS behind stable contracts → swappable implementations
- **Sub-300ms Latency**: Streaming, pre-rendered fillers, filler injection race with NLP
- **Graceful Degradation**: RAG, filler, Groq, and CRM failures degrade instead of crash
- **Exactly-Once Finalization**: Every call persisted exactly once, even on drops

---

## Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | React 18 + Vite + Tailwind CSS | SPA dashboard + real WS client |
| **Backend** | Python 3.11, FastAPI | Async orchestrator + microservices |
| **Transport** | WebSocket + REST | Full-duplex audio + JSON envelopes |
| **AI/NLP** | **Real** Groq `llama-3.3-70b-versatile` | Live intent classification + response |
| **STT** | **Real** Sarvam Saaras (primary) → Groq Whisper (fallback) → Stub | Speech-to-text with language detection |
| **TTS** | **Real** Multi-provider routing: Sarvam Bulbul (Hindi/Hinglish) → NVIDIA Magpie (Hindi fallback) → Groq Orpheus (English) → Stub | Tone-aware text-to-speech synthesis |
| **State** | Redis 7 | Session state with TTL |
| **Data** | PostgreSQL 16 + pgvector | Calls, transcripts, bots, customers, RAG |
| **Observability** | OpenTelemetry + structured JSON logs | Distributed tracing + correlation IDs |
| **Auth** | JWT (HS256) | REST + WebSocket bearer tokens |
| **Infra (dev)** | Docker Compose | One image, five uvicorn entrypoints |
| **Infra (prod)** | AWS ap-south-1 (target) | EC2 + NVIDIA NVCF (Magpie) + Sarvam APIs + Groq |

---

## Data Models & Database Schema

### Entity-Relationship Diagram

```
CrmCustomer (1) ──→ (N) CallLog
BotConfig   (1) ──→ (N) CallLog
CallLog     (1) ──→ (N) TranscriptTurn
KnowledgeChunk (N) ──→ (1) BotConfig (logical via rag_index)
```

### Core Tables

#### `crm_customers`
Customer records for outbound campaigns.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `org_id` | String | Tenant scope (nullable for P1 single-tenant) |
| `name` | String(120) | **PII** — masked in logs |
| `phone` | String(20) | Dialing target |
| `language` | String(16) | Default language (en, hi, hinglish) |
| `bank` | String(80) | Bank name (RBI disclosure) |
| `use_case` | Enum | credit_card, collections, personal_loan, insurance, custom |
| `attributes` | JSON | Use-case-specific fields (offer, DPD, EMI, premium, etc.) |
| `dnd` | Boolean | TRAI DND flag; 409 blocks session start |
| `created_at` | DateTime | Record creation timestamp |

#### `bot_configs`
Bot instances — the product's moat. New use cases = new config rows.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `org_id` | String | Tenant scope |
| `name` | String(120) | Display name |
| `use_case` | Enum | Determines script templates, RAG index, rules |
| `language` | String(16) | Bot's primary language |
| `voice_gender` | String(16) | female, male, neutral |
| `speaking_rate` | Float | 0.8–1.2 multiplier on synthesis |
| `greeting_template` | Text | Jinja2-style: `{customer.name}`, `{offer.amount}` |
| `rag_enabled` | Boolean | Fetch contextual knowledge at NLP time |
| `rag_index` | String(64) | Logical namespace in `knowledge_chunks` |
| `rag_top_k` | Int | Override NLP_svc default (W) |
| `rag_score_threshold` | Float | Override minimum relevance (W) |
| `default_tone` | String(16) | neutral, empathetic, urgent, supportive |
| `ambient_profile` | String(16) | callfloor, office, home (audio bed style) |
| `config` | JSON | CRM field mapping, backchannel overrides, escalation rules |
| `active` | Boolean | Enable/disable bot without deletion |
| `created_at` | DateTime | Record creation |

#### `call_logs`
One row per call, written on session end.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `org_id` | String | Tenant scope |
| `session_id` | String(64) | Maps to Redis session; indexed for reconnects |
| `bot_id` | UUID FK | Foreign key to `bot_configs` |
| `customer_id` | UUID FK | Foreign key to `crm_customers` (nullable) |
| `direction` | Enum | outgoing (default), incoming |
| `language` | String(16) | Language spoken during call |
| `outcome` | Enum | converted, declined, escalated, no_answer, customer_unresponsive, call_dropped, unsupported_language, in_progress |
| `started_at` | DateTime | Call initiation |
| `ended_at` | DateTime | Call termination |
| `duration_ms` | Int | Total duration |
| `escalated` | Boolean | Handed off to human |
| `recording_url` | String | S3 object path (or null in MVP) |
| `metrics` | JSON | `{turn_count, consent_captured, consent_at, trace_id}` |

#### `transcript_turns`
One row per bot/customer utterance (cascade delete on call).

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `call_id` | UUID FK | Foreign key to `call_logs` |
| `turn_index` | Int | 0-based turn number |
| `speaker` | String | bot \| customer |
| `text` | Text | Transcribed/generated utterance |
| `language` | String(16) | Language of this turn (mid-call switch possible) |
| `intent` | String | NLP-classified intent (high_interest, objection, opt_out, etc.) |
| `tone` | String(16) | Bot-assigned tone for TTS (neutral, empathetic, urgent) |
| `interrupted` | Boolean | True if barge-in cut this turn short |
| `confidence` | Float | STT/NLP confidence (0–1) |

#### `knowledge_chunks`
RAG knowledge base with pgvector embeddings.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `rag_index` | String(64) | Logical namespace scoped to a bot/use-case |
| `use_case` | Enum | Filter context |
| `language` | String(16) | Language of chunk |
| `title` | String | Short label for QA |
| `content` | Text | Full knowledge (FAQ, product details, script variation) |
| `embedding` | Vector(1024) | pgvector embedding; computed at insert or on upload |

---

## REST API Reference

All endpoints require JWT bearer token unless noted. Base URL: `http://localhost:8100` (dev).

### Authentication

#### `POST /v1/auth/login`
**Auth**: None  
**Request**:
```json
{
  "username": "demo",
  "password": "koel-demo"
}
```
**Response** (200):
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer"
}
```

#### `POST /v1/auth/ws-token`
**Auth**: Bearer access_token  
**Response** (200): Short-lived WebSocket token (120s TTL)
```json
{
  "ws_token": "eyJ0eXAiOiJKV1QiLCJraW5kIjoiY3MifQ..."
}
```

### Bot Management

#### `GET /v1/bots`
**Auth**: Bearer access_token  
**Query**: None  
**Response** (200):
```json
{
  "bots": [
    {
      "id": "abc123def456",
      "name": "CC Cross-Sell Bot",
      "use_case": "credit_card",
      "language": "hinglish",
      "voice_gender": "female",
      "active": true
    }
  ]
}
```

#### `POST /v1/bots`
**Auth**: Bearer access_token  
**Request**:
```json
{
  "name": "Personal Loan Bot",
  "use_case": "personal_loan",
  "language": "hinglish",
  "greeting_template": "Hello {customer.name}, calling about a personal loan offer"
}
```
**Response** (201): Bot config object

#### `GET /v1/bots/{bot_id}`
**Auth**: Bearer access_token  
**Response** (200): Single bot config, or 404

### Session Management

#### `POST /v1/sessions`
**Auth**: Bearer access_token  
**Request**:
```json
{
  "bot_id": "abc123def456",
  "customer_id": "cust789xyz"
}
```
**Response** (200):
```json
{
  "session_id": "sess_abc123",
  "ws_token": "eyJ0eXAiOiJKV1QiLCJraW5kIjoid3MifQ...",
  "customer_context": {
    "name": "R. S.",
    "phone": "+91-XXXXX7890",
    "bank": "HDFC Bank"
  }
}
```

**Possible Errors**:
- **409 Conflict**: Customer on DND list
- **403 Forbidden**: Outside calling hours (if `ENFORCE_CALLING_HOURS=true`)
- **404 Not Found**: Bot inactive or missing

### Call History

#### `GET /v1/calls`
**Auth**: Bearer access_token  
**Query**:
- `bot_id`: Filter by bot
- `outcome`: Filter by call outcome
- `language`: Filter by language
- `limit`: Max results (default 50, max 200)
- `offset`: Pagination offset

**Response** (200):
```json
{
  "calls": [
    {
      "id": "call_abc123",
      "session_id": "sess_xyz",
      "bot_id": "bot_def456",
      "customer_id": "cust_789",
      "language": "hinglish",
      "outcome": "converted",
      "started_at": "2026-07-07T10:30:00Z",
      "ended_at": "2026-07-07T10:45:15Z",
      "duration_ms": 915000,
      "escalated": false
    }
  ],
  "total": 42
}
```

#### `GET /v1/calls/{call_id}`
**Auth**: Bearer access_token  
**Response** (200):
```json
{
  "call": { /* call object */ },
  "transcript": [
    {
      "turn_index": 0,
      "speaker": "bot",
      "text": "Hello Rahul, thank you for answering...",
      "tone": "neutral",
      "language": "hinglish"
    },
    {
      "turn_index": 1,
      "speaker": "customer",
      "text": "Hi, who is this?",
      "intent": "unknown",
      "confidence": 0.92
    }
  ]
}
```

### Internal CRM API (service-to-service, `role=service`)

#### `GET /v1/customers`
**Auth**: Service principal JWT  
**Response** (200): List of all customers

#### `GET /v1/customers/{customer_id}`
**Auth**: Service principal JWT  
**Response** (200): Single customer record

#### `POST /v1/customers/{customer_id}/flags`
**Auth**: Service principal JWT  
**Request**:
```json
{
  "flag": "callback",
  "reason": "customer_unresponsive"
}
```
**Response** (201)

---

## WebSocket Protocol

### Connection Flow

```
Client                          Server
  |                              |
  |-- POST /v1/sessions ------→  | (fetch ws_token, context)
  |← session_id + ws_token ------|
  |                              |
  |-- WS /ws/calls/{id}?token=x→ | (validate token + owner)
  |← 101 Upgrade ---|            |
  |← CALL_STATE     |            | (connecting)
  |← TRANSCRIPT     |            | (greeting)
  |← AUDIO_CHUNK    |            | (greeting audio)
  |← PING           |            | (every 5s heartbeat)
  |                              |
  |-- AUDIO_CHUNK ──→            | (customer speech, is_final=true)
  |← TRANSCRIPT ────|            | (STT result)
  |← AUDIO_CHUNK    |            | (or FILLER if slow)
  |← TRANSCRIPT ────|            | (NLP response)
  |← AUDIO_CHUNK    |            | (bot speech)
  |← CALL_STATE ────|            | (listening)
  |                              |
  |-- PONG ──────────→           | (heartbeat)
  |                              |
  |-- END_CALL ──────→           | (user hangup)
  |← CALL_ENDED ────|            | (outcome)
  |-- (close 1000)  |            |
```

### Message Types

#### Client → Server

**`AUDIO_CHUNK`**
```json
{
  "type": "audio_chunk",
  "data": {
    "audio_b64": "//NExAAXMAx1AAQQABAQADgEBAQAA...",
    "is_final": false,
    "sample_rate": 16000
  }
}
```

**`BARGE_IN_CANDIDATE`** (while bot speaks)
```json
{
  "type": "barge_in_candidate",
  "data": {
    "partial_text": "No thanks",
    "speech_duration_ms": 450,
    "language": "en"
  }
}
```

**`PONG`**
```json
{
  "type": "pong"
}
```

**`END_CALL`**
```json
{
  "type": "end_call"
}
```

#### Server → Client

**`CALL_STATE`**
```json
{
  "type": "call_state",
  "data": {
    "status": "listening",
    "reason": null
  }
}
```

**`TRANSCRIPT`**
```json
{
  "type": "transcript",
  "data": {
    "speaker": "bot",
    "text": "That sounds good. When would you like to proceed?",
    "tone": "neutral",
    "index": 3,
    "language": "hinglish"
  }
}
```

**`AUDIO_CHUNK`**
```json
{
  "type": "audio_chunk",
  "data": {
    "audio_b64": "//NExAAXMAx1AAQQABAQADgEBAQAA...",
    "duration_ms": 2400,
    "index": 3
  }
}
```

**`FILLER`** (injected when NLP is slow)
```json
{
  "type": "filler",
  "data": {
    "text": "Let me check that for you.",
    "audio_b64": "//NExAAXMAx1AAQQABAQADgEBAQAA...",
    "tone": "neutral"
  }
}
```

**`TTS_KILL`** (barge-in interrupt)
```json
{
  "type": "tts_kill",
  "data": {
    "reason": "backchannel",
    "stop_target_ms": 150
  }
}
```

**`RECONNECTED`**
```json
{
  "type": "reconnected",
  "data": {
    "text": "You've been reconnected.",
    "last_turn": 3
  }
}
```

**`PING`**
```json
{
  "type": "ping"
}
```

**`ERROR`**
```json
{
  "type": "error",
  "data": {
    "code": "nlp_timeout",
    "message": "AI service timed out"
  }
}
```

**`CALL_ENDED`**
```json
{
  "type": "call_ended",
  "data": {
    "outcome": "converted",
    "escalated": false,
    "turns": 12,
    "trace_id": "abc123def456"
  }
}
```

---

## Core Components

### 1. VoiceBot Orchestrator (`koel/voicebot/`)

The central hub that owns conversation state and orchestrates the pipeline.

**Key modules**:
- **`main.py`**: FastAPI app, WebSocket endpoint, REST API routes
- **`config.py`**: `VoiceBotSettings` with resilience thresholds
- **`session/`**: `SessionState` (Pydantic) + Redis store
- **`ws/handler.py`**: `CallSession` class; owns one socket
- **`pipeline/orchestrator.py`**: Transport-agnostic orchestration logic
- **`pipeline/bargein.py`**: Dual-gate barge-in classifier
- **`pipeline/filler.py`**: Filler cache + rotation
- **`pipeline/silence.py`**: No-input timeout logic
- **`api/`**: REST endpoints (auth, bots, sessions, history)

**Responsibilities**:
- Session lifecycle (create, resume, finalize)
- WebSocket upgrade & connection management
- Per-turn pipeline: STT → NLP → TTS
- Barge-in detection & interrupt
- Filler injection when NLP is slow
- No-input re-prompt & close
- Graceful reconnect within 15s window
- Exactly-once call finalization

### 2. Speech-to-Text Service (`koel/stt/`)

Converts audio to text + detects language with automatic fallback chain.

**Status**: **Real Sarvam Saaras** (primary) with graceful fallback  
**Request**: `TranscribeRequest{session_id, audio_b64, sample_rate, is_final, language_hint, audio_format}`  
**Response**: `TranscriptResult{text, is_final, language_id, language_confidence, speech_duration_ms, word_confidence}`

**Provider Chain** (automatic fallback on error):
1. **Sarvam Saaras** (primary) — Trained on Indian code-mixed speech (EN/HI), auto-detects language, returns genuine `language_probability` for unsupported-language escalation
2. **Groq Whisper** (fallback) — Language-pinned via config (en/hi forced per bot; hinglish → hi)
3. **StubWhisperProvider** (ultimate fallback) — Type-to-talk (prefix with `TEXT:`) or deterministic demo utterances

**Language codes**:
- Sarvam: `hi-in` → `Language.hi`, `en-in` → `Language.en`, others → `Language.other`
- **Key feature**: Sarvam runs in `language_code="unknown"` (not forced) so it returns actual confidence for escalation gating
- Type-to-talk: prefix audio with `TEXT:` to return verbatim (for demo/keyboard input)

### 3. Natural Language Processing (`koel/nlp/`)

Intent classification, response generation, tone assignment, RAG retrieval.

**Status**: **Real Groq (`llama-3.3-70b-versatile`)**, stub fallback  
**Request**: `TurnContext{session_id, bot_id, use_case, language, customer_text, history, crm_context, rag_enabled, rag_index}`  
**Response**: `NlpResponse{response_text, intent, tone, escalate, end_call, confidence, retrieved[]}`

**Groq integration** (`providers.GroqNlpProvider`):
- System prompt: intent/tone enums, anti-hallucination rules, JSON contract
- User prompt: use case, language, CRM context, RAG chunks (top-5), last-8 history
- POST to Groq OpenAI-compatible endpoint with `response_format=json_object`
- Coerces intent/tone to valid enums; fallback on exception

**RAG** (`rag.py`):
- `HashEmbedder` (deterministic stub) or Bedrock Titan (extension seam)
- pgvector cosine-distance search over `knowledge_chunks` by `rag_index`
- Best-effort; failures degrade gracefully

**Intents** (11): `high_interest, interested, objection, not_interested, busy, promise_to_pay, dispute, request_human, opt_out, smalltalk, unknown`

**Tones**: `neutral, empathetic, urgent, supportive`

### 4. Text-to-Speech Service (`koel/tts/`)

Synthesizes speech with tone-aware prosody + ambient bed. Multi-provider routing by language.

**Status**: **Real Sarvam Bulbul (Hindi/Hinglish) + NVIDIA Magpie (Hindi fallback) + Groq Orpheus (English)**  
**Request**: `SynthesizeRequest{text, language, tone, voice_gender, speaking_rate, stream, ambient, ambient_profile}`  
**Response**: Streamed WAV audio (~120ms chunks for barge-in boundaries)

**Provider Routing** (`RoutingTtsProvider`):
| Language | Primary | Fallback 1 | Fallback 2 | Ultimate |
|----------|---------|-----------|-----------|----------|
| **Hindi** | Sarvam Bulbul (romanized-native) | NVIDIA Magpie (Devanagari-native, NVCF gRPC) | Groq Orpheus | Stub |
| **Hinglish** | Sarvam Bulbul (verified 1.0 confidence round-trip with STT) | Groq Orpheus (not Magpie — mangles romanized) | — | Stub |
| **English** | Groq Orpheus | Stub | — | Stub |

**Tone → Prosody** (`TONE_PROSODY` mapping):
- Neutral: pitch=100%, rate=1.0×, energy=100%
- Empathetic: pitch=98%, rate=0.95×, energy=102%
- Urgent: pitch=102%, rate=1.05×, energy=105%
- Supportive: pitch=99%, rate=0.98×, energy=101%

**Ambient bed** (`_ambient_pcm`):
- Deterministic LCG noise generator (reproducible for testing)
- Mixed under TTS when `ambient=True`
- Muted on escalation handoff

**Streaming**:
- Sarvam/Groq: sentence-level streaming (Tier 2 — plays first sentence while rest synthesizes)
- NVIDIA Magpie: also sentence-level (future: `synthesize_online` for true sub-sentence streaming)

### 5. CRM Service (`koel/crm/`)

Synthetic customer database. Swappable for real CRM via same contract.

**Endpoints** (all require `ServicePrincipal`):
- `GET /v1/customers` — list all
- `GET /v1/customers/{id}` — single record
- `POST /v1/customers/{id}/flags` — set flag (opt_out, callback, dnd)
- `POST /v1/customers/{id}/interactions` — log interaction

**Seed data** (`seed.py`): 5 synthetic profiles, one DND record

---

## Configuration & Environment Variables

All settings are `KOEL_`-prefixed and read from `.env` (via `pydantic-settings`).

| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `ENVIRONMENT` | str | development | env tag (development, staging, production) |
| `DEBUG` | bool | true | debug logging |
| `AWS_REGION` | str | ap-south-1 | data residency (RBI compliance) |
| **Auth** | | | |
| `JWT_SECRET` | str | dev-secret-change-in-prod | HS256 signing key; fail-fast if unchanged in prod |
| `JWT_ALGORITHM` | str | HS256 | token algorithm |
| `JWT_ACCESS_TTL_SECONDS` | int | 3600 | access token TTL (1 hour) |
| `JWT_WS_TTL_SECONDS` | int | 120 | WebSocket token TTL (2 minutes) |
| **Data** | | | |
| `DATABASE_URL` | str | postgresql://localhost/koel | SQLAlchemy URL |
| `REDIS_URL` | str | redis://localhost:6379/0 | Session store |
| **AI Services** | | | |
| `STT_SVC_URL` | str | http://localhost:8101 | Speech-to-text endpoint |
| `NLP_SVC_URL` | str | http://localhost:8102 | NLP endpoint |
| `TTS_SVC_URL` | str | http://localhost:8103 | TTS endpoint |
| `CRM_SVC_URL` | str | http://localhost:8104 | CRM endpoint |
| **STT Provider** | | | |
| `STT_PROVIDER` | str | stub | stub \| groq_whisper \| sarvam |
| `SARVAM_API_KEY` | str | — | Sarvam API key (required if provider=sarvam) |
| `SARVAM_STT_MODEL` | str | saaras:v3 | Sarvam STT model ID |
| `SARVAM_STT_BASE_URL` | str | https://api.sarvam.ai | Sarvam API endpoint |
| **NLP** | | | |
| `NLP_PROVIDER` | str | stub | stub \| groq |
| `GROQ_API_KEY` | str | — | Groq API key (required if provider=groq) |
| `GROQ_BASE_URL` | str | https://api.groq.com/openai/v1 | Groq endpoint |
| `GROQ_MODEL` | str | llama-3.3-70b-versatile | Model ID |
| `GROQ_TEMPERATURE` | float | 0.3 | Sampling temperature |
| `GROQ_MAX_TOKENS` | int | 512 | Max output tokens |
| `GROQ_TIMEOUT_S` | float | 8.0 | Timeout for API call |
| **TTS Provider** | | | |
| `TTS_PROVIDER` | str | stub | stub \| groq_orpheus \| sarvam (+ routing) |
| `SARVAM_TTS_API_KEY` | str | — | Sarvam API key (required if provider=sarvam) |
| `SARVAM_TTS_MODEL` | str | bulbul:v3 | Sarvam TTS model (Bulbul for Hindi/Hinglish) |
| `SARVAM_TTS_SPEAKER` | str | meera | Voice speaker profile |
| `SARVAM_TTS_BASE_URL` | str | https://api.sarvam.ai | Sarvam API endpoint |
| `TTS_HINDI_PROVIDER` | str | magpie | Provider for Hindi TTS (magpie \| orpheus) |
| `TTS_HINDI_LANGUAGES` | str | hi,hinglish | Languages routed to Hindi provider |
| `NVIDIA_API_KEY` | str | — | NVIDIA NVCF API key (required if using Magpie) |
| `NVIDIA_MAGPIE_FUNCTION_ID` | str | — | NVIDIA NVCF function ID for Magpie |
| `NVIDIA_MAGPIE_URI` | str | nvcf.nvidia.com | NVIDIA NVCF endpoint |
| `NVIDIA_MAGPIE_VOICE` | str | hindi-male | Voice for Magpie TTS |
| `NVIDIA_MAGPIE_SAMPLE_RATE` | int | 24000 | Sample rate for Magpie audio |
| `GROQ_ORPHEUS_MODEL` | str | distil-whisper-large-v3-en | Groq TTS model (English) |
| `GROQ_ORPHEUS_VOICE` | str | autumn | Groq Orpheus voice (autumn/austin) |
| **Resilience** | | | |
| `TURN_LATENCY_BUDGET_MS` | int | 300 | Target conversational latency |
| `FILLER_TRIGGER_MS` | int | 300 | Inject filler if NLP slower |
| `BACKCHANNEL_MAX_SPEECH_MS` | int | 300 | Max duration before interrupt |
| `BARGE_IN_STOP_TARGET_MS` | int | 150 | Grace period for TTS stop |
| `NO_INPUT_REPROMPT_MS` | int | 5000 | First silence timeout |
| `NO_INPUT_CLOSE_MS` | int | 5000 | Second silence timeout |
| `SESSION_GRACE_TTL_SECONDS` | int | 15 | Reconnect window after drop |
| `SESSION_ACTIVE_TTL_SECONDS` | int | 3600 | Active session Redis TTL |
| `HEARTBEAT_INTERVAL_SECONDS` | int | 5 | Ping interval |
| `HEARTBEAT_TIMEOUT_SECONDS` | int | 12 | Pong timeout before close |
| `LANGUAGE_CONFIDENCE_THRESHOLD` | float | 0.6 | Escalate if confidence < threshold |
| **RBI Compliance** | | | |
| `ENFORCE_CALLING_HOURS` | bool | false | Block out-of-hours calls |
| `CALLING_HOURS_START` | int | 9 | Start hour (24h) |
| `CALLING_HOURS_END` | int | 21 | End hour (24h) |

### Example `.env` (Development)

```bash
ENVIRONMENT=development
DEBUG=true
DATABASE_URL=postgresql://localhost/koel
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=dev-secret-change-in-prod

# STT (defaults to stub)
STT_PROVIDER=sarvam
SARVAM_API_KEY=your-sarvam-key
SARVAM_STT_MODEL=saaras:v3

# NLP
NLP_PROVIDER=groq
GROQ_API_KEY=your-groq-key
GROQ_MODEL=llama-3.3-70b-versatile

# TTS (Sarvam primary, Groq fallback)
TTS_PROVIDER=sarvam
SARVAM_TTS_API_KEY=your-sarvam-key
SARVAM_TTS_MODEL=bulbul:v3
TTS_HINDI_PROVIDER=magpie
TTS_HINDI_LANGUAGES=hi,hinglish
NVIDIA_API_KEY=your-nvidia-nvcf-key
NVIDIA_MAGPIE_FUNCTION_ID=your-function-id
```

### Example `.env` (Production)

```bash
ENVIRONMENT=production
DEBUG=false
AWS_REGION=ap-south-1
DATABASE_URL=postgresql://prod-user:pwd@prod-rds.ap-south-1.rds.amazonaws.com:5432/koel
REDIS_URL=redis://prod-redis.ap-south-1.elasticache.amazonaws.com:6379/0

# Secrets (use AWS Secrets Manager in prod, NOT in .env!)
JWT_SECRET=generate-strong-secret-rotate-quarterly

# STT (Sarvam primary)
STT_PROVIDER=sarvam
SARVAM_API_KEY=${SARVAM_API_KEY}  # from AWS Secrets Manager
SARVAM_STT_MODEL=saaras:v3

# NLP (Groq primary)
NLP_PROVIDER=groq
GROQ_API_KEY=${GROQ_API_KEY}  # from AWS Secrets Manager
GROQ_MODEL=llama-3.3-70b-versatile

# TTS (Sarvam + NVIDIA + Groq cascade)
TTS_PROVIDER=sarvam
SARVAM_TTS_API_KEY=${SARVAM_API_KEY}
SARVAM_TTS_MODEL=bulbul:v3
TTS_HINDI_PROVIDER=magpie
TTS_HINDI_LANGUAGES=hi,hinglish
NVIDIA_API_KEY=${NVIDIA_API_KEY}  # from AWS Secrets Manager
NVIDIA_MAGPIE_FUNCTION_ID=your-prod-function-id
NVIDIA_MAGPIE_URI=nvcf.nvidia.com

# Compliance
ENFORCE_CALLING_HOURS=true
CALLING_HOURS_START=9
CALLING_HOURS_END=21
```

⚠️ **Never commit real API keys to git.** Use:
- AWS Secrets Manager in production
- `.env.local` (git-ignored) in development
- Environment variable injection in Docker/Kubernetes

---

## Deployment Guide

### Development (Docker Compose)

**Prerequisites**: Docker, Docker Compose

```bash
cd backend
docker-compose up
```

This starts:
- PostgreSQL + pgvector (5432)
- Redis (6379)
- Five uvicorn services in one image:
  - voicebot (8100)
  - stt (8101)
  - nlp (8102)
  - tts (8103)
  - crm (8104)

**Database initialization**:
```bash
python scripts/bootstrap_db.py
```

Creates tables, seeds bots + customers, loads RAG knowledge.

### Production (AWS EC2 + SageMaker)

**Target**: ap-south-1 (data residency)

**Services**:
- **App**: EC2 instances running the same Docker image (horizontally scaled)
- **STT**: Sarvam Saaras API (live) + Groq Whisper (fallback)
- **TTS**: Sarvam Bulbul API (Hindi/Hinglish) + NVIDIA NVCF Magpie (Hindi gRPC) + Groq Orpheus (English)
- **NLP**: Groq API (live) with Bedrock/Sagemaker fallback for embeddings
- **Database**: RDS PostgreSQL 16 with pgvector extension (ap-south-1 for RBI compliance)
- **Session Store**: ElastiCache Redis (ap-south-1)
- **Storage**: S3 for call recordings with region-locked policies + 30-day lifecycle (⚠️ not yet implemented)
- **Logging**: CloudWatch with OpenTelemetry exporter
- **Load Balancing**: ALB with sticky sessions disabled (stateless by design)
- **Secrets**: AWS Secrets Manager for API keys (Groq, Sarvam, NVIDIA, JWT secret)

**Deployment checklist**:
- [ ] Change `JWT_SECRET` from dev default
- [ ] Set `ENVIRONMENT=production`, `DEBUG=false`
- [ ] Configure RDS PostgreSQL URL
- [ ] Configure ElastiCache Redis URL
- [ ] Set Groq API key or Bedrock IAM role
- [ ] Enable `ENFORCE_CALLING_HOURS`
- [ ] Configure S3 bucket for recordings
- [ ] Set up CloudWatch + OTel endpoint
- [ ] Run `scripts/bootstrap_db.py` once on first deploy
- [ ] Run database migrations: `alembic upgrade head`

### Horizontal Scaling

No sticky sessions needed:
- All call state in Redis with TTL
- Reconnect can land on any VB instance
- Provider calls are stateless

**Load testing**: See `scripts/loadtest.py` (targets 10 concurrent sessions).

### Production Readiness Status

**Current**: ~80% code maturity / 10% ops maturity  
**Launch Blockers** (from PRODUCTION_MASTERPLAN.md):

- ⚠️ **Secret exposure**: Groq/NVIDIA/Sarvam API keys in `.env` → must rotate and use AWS Secrets Manager in prod
- ⚠️ **Data residency**: Database must run in ap-south-1 (RBI compliance); S3 recordings must use region-locked policies
- ⚠️ **Telecom onboarding**: Not yet started; integration with TRAI/RBI requires bank legal review of disclosure + consent flow
- ⚠️ **Frontend debt**: `App.tsx` = 3,466 lines; no state library (Zustand/Jotai); strict:false in tsconfig
- ⚠️ **Recording storage**: S3 URL generation + 30-day retention cleanup not yet implemented
- ⚠️ **Real audio client**: Frontend demo uses type-to-talk; real microphone + browser mic capture seam (`src/lib/audio.ts` has stubs)

**Fully Production-Ready**:
- ✅ Database schema + migrations (Alembic)
- ✅ Authentication (JWT + WS token validation)
- ✅ Orchestration logic + resilience (barge-in, filler, grace reconnect, etc.)
- ✅ AI provider routing (Sarvam → Groq → Stub)
- ✅ Call persistence + audit trail
- ✅ Structured logging + OTel tracing
- ✅ DPDP/RBI/TRAI/IRDAI compliance framework (enforcement ready, legal review pending)

---

## Development Workflow

### Running Tests

```bash
# Unit + integration tests
pytest -v

# With coverage
pytest --cov=koel --cov-report=term-missing

# Lint
ruff check koel/

# Type check
mypy koel/

# Format
black koel/
ruff check --fix koel/
```

### Database Migrations

Using Alembic (tracked in `migrations/versions/`):

```bash
# Auto-generate migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

### Live Testing Scripts

```bash
# Happy path: login → session → call → history
python scripts/demo_call.py

# Resilience: drop/reconnect, no-input, language fallback
python scripts/demo_resilience.py

# Load test (target 10 concurrent calls)
python scripts/loadtest.py --concurrent=10 --duration=60
```

### Debugging

Enable structured JSON logs with correlation ID:

```python
from koel.common.telemetry import span

with span("my_operation", bot_id=bot_id):
    # code
    pass
```

View traces in OpenTelemetry exporter (Jaeger, Datadog, etc.).

---

## Extension Points (Seams & Future Work)

| Feature | Status | Where | Notes |
|---------|--------|-------|-------|
| **Sarvam Saaras STT** | ✅ Live | `koel/stt/providers.py` | `SarvamSttProvider` fully implemented with fallback to Groq Whisper → Stub |
| **Sarvam Bulbul TTS** | ✅ Live | `koel/tts/providers.py` | `SarvamTtsProvider` for Hindi/Hinglish; handles romanized text natively |
| **NVIDIA Magpie TTS** | ✅ Live | `koel/tts/providers.py` | `NvidiaMagpieTtsProvider` via NVCF gRPC for Hindi (Devanagari-native) |
| **Groq Whisper STT** | ✅ Live | `koel/stt/providers.py` | `GroqWhisperProvider` as fallback with language-pinning per bot config |
| **Groq Orpheus TTS** | ✅ Live | `koel/tts/providers.py` | `GroqOrpheusProvider` for English (terms gate in Groq console) |
| **TTS Language Routing** | ✅ Live | `koel/tts/providers.py` | `RoutingTtsProvider` routes Hindi/Hinglish → Sarvam (→ Magpie), English → Orpheus |
| **Real SageMaker Whisper** | 🔲 Seam | `koel/stt/providers.py` (line 333) | `WhisperSageMakerProvider` stub; implement `.transcribe()` with boto3 sagemaker-runtime invoke_endpoint |
| **Real embeddings** | 🔲 Seam | `koel/nlp/rag.py` | Replace `HashEmbedder` (deterministic stub) with Bedrock Titan / OpenAI Ada / Nomic Embed |
| **PSTN Telephony** | 🔲 Future | (new module) | Transport-agnostic: new module calls same `Orchestrator` (needs only `send: ServerMessage → Awaitable` callback); Twilio/Exotel bridge |
| **Custom use case** | ✅ Config | `POST /v1/bots` | No code — define bot config, greeting template, RAG index, CRM field mapping, backchannels |
| **Multi-tenancy** | 🔲 Scaffolded | `koel/db/models.py` | `org_id` fields exist on CrmCustomer, BotConfig, CallLog, BotConfig; index in place; backfill to activate |
| **Real CRM** | 🔲 Swappable | `koel/crm/main.py` | Contract is simple: GET /v1/customers/{id}, POST /flags, POST /interactions; dummy implements all three |
| **Analytics & performance** | ✅ Partial | `koel/voicebot/api/analytics.py` | Endpoint exists; can expand with per-bot metrics, intent distribution, language distribution, outcome breakdown |

---

## Recent Features & Enhancements (Post-MVP)

The codebase includes several advanced features built on the core architecture:

### 1. **Silero Neural VAD + RMS Fallback** (commit `952a56f`, `c521ab5`)
- **Silero v5** model in browser (client-side VAD)
- **Adaptive RMS threshold** fallback for robustness (not just a fixed threshold)
- Gate 1 of barge-in → `barge_in_candidate` triggers only on genuine speech
- Reduces false-interrupt rate on silence, noise, or backchannel tones

### 2. **Sarvam Transliteration** (commit `8b8388e`)
- **`POST /v1/nlp/transliterate`** endpoint
- Converts Hinglish (romanized) ↔ Devanagari (script) bidirectionally
- Collapses dual-script maintenance into one canonical request/response
- Used for RAG context, bot responses, and logging

### 3. **Post-Call CRM Summarizer** (commit `6af0c55`)
- **Sarvam Chat Completions** (not just STT/TTS)
- Auto-generates brief call summary from transcript
- Flags for CRM follow-up (callback, escalation reason, next step)
- Stored in CallLog.metrics for compliance & QA

### 4. **Knowledge-Base File Upload** (commit `549a0bc`)
- **`POST /v1/knowledge/upload`** ingest PDF + text files
- Extracts text, chunks, embedds (HashEmbedder or real embeddings), stores in pgvector
- No manual RAG setup required — upload, it's queryable

### 5. **Semantic End-of-Turn Gate** (commit `e1d6c38`)
- Beyond duration/silence: detect actual conversational completeness
- Prevents premature NLP trigger on incomplete customer thoughts
- Works alongside existing backchannel + duration gates

### 6. **Adaptive Noise Floor for VAD** (commit `c521ab5`)
- Per-call RMS floor estimation from initial silence segment
- Reduces false-positives in high-ambient-noise calls (call centers, streets)
- Trades latency (brief calibration segment) for accuracy

---

## Key Algorithms (Summary)

### 1. Dual-Gate Barge-In
- **Gate 1**: Client VAD fires `barge_in_candidate` while bot speaks
- **Gate 2**: Classify by duration + dictionary
  - `< backchannel_max_speech_ms` → ignore (too short)
  - Normalized text ∈ per-language backchannel dict → ignore
  - Else → interrupt, send `TTS_KILL`

### 2. Filler Racing
```python
nlp_task = asyncio.create_task(nlp.respond(...))
try:
    response = await asyncio.wait_for(
        asyncio.shield(nlp_task),
        timeout=filler_trigger_ms / 1000
    )
except asyncio.TimeoutError:
    # Inject filler, await NLP anyway
    await send(filler_clip)
    response = await nlp_task
```

### 3. Pong-Independent No-Input
- Timer starts at `_last_input_at`
- **Pongs don't reset it** (heartbeat is independent)
- Real audio frames reset the timer
- Reprompt extends window; close after 2 strikes

### 4. Dropped-Call Finalizer
- Socket drop → status `reconnecting`, TTL 15s
- Detached async task: `_await_grace_then_finalize()`
- Reconnect within 15s → task sees status change, no-op
- After 15s → persist call (outcome=`call_dropped`), flag CRM for callback

### 5. Language Fallback
- STT returns `language_id` + confidence
- If confidence < threshold AND unsupported → apologise + escalate + tag

---

## Quick Reference: Key Files

| File | Purpose |
|------|---------|
| `backend/koel/voicebot/main.py` | FastAPI app + WS endpoint |
| `backend/koel/voicebot/pipeline/orchestrator.py` | Core conversation logic |
| `backend/koel/voicebot/ws/handler.py` | CallSession WebSocket handler |
| `backend/koel/nlp/providers.py` | Groq + stub NLP |
| `backend/koel/db/models.py` | SQLAlchemy schema |
| `backend/koel/common/auth.py` | JWT issue/decode |
| `backend/docker-compose.yml` | Dev infrastructure |
| `src/App.tsx` | React frontend |
| `src/lib/koel.ts` | Koel client library |

---

## Support & Contact

For technical issues or clarifications, refer to:
- **HLD**: `docs/HLD.md` (system architecture, flows)
- **LLD**: `docs/LLD.md` (code-level detail, algorithms)
- **Security**: `docs/SECURITY.md` (auth, encryption, compliance)
- **Deployment**: `docs/DEPLOY.md` (infra, SLOs)

---

**Document Version**: 1.2.0  
**Last Sync**: 2026-07-07  
**Status**: Current with codebase
