# KOEL — Deploy (Render)

KOEL is **two deployables**: the React **frontend** (static site) and the FastAPI
**backend** (Docker web service + AI/CRM services). They must be deployed
separately and pointed at each other. Region: **Ohio (US East)** so the backend
is co-located with Groq/NVIDIA (US-local inference, not trans-Pacific).

> **Current state:** only the frontend is deployed
> (`koel-voice-bot.onrender.com`), and it was built **without** `VITE_KOEL_API`,
> so it talks to `localhost:8100` → login fails → the app runs in **offline demo
> mode** (simulated latency, no real STT/NLP/TTS). Deploy the backend and set
> `VITE_KOEL_API` to make it a real call.

## 1. Frontend (static site) — what you have, plus the one fix

| Setting | Value |
|---|---|
| Type | Static Site (or Web Service running `npx serve -s dist -l $PORT`) |
| Region | Ohio |
| Build command | `npm install && npm run build` |
| Publish dir | `dist` |
| **Env var (build-time!)** | `VITE_KOEL_API = https://<backend-service>.onrender.com` |

Vite inlines `VITE_KOEL_API` at build time (`src/lib/koel.ts`), and the WebSocket
base is derived from it (`http→ws`). **Without this var the FE points at
localhost.** After setting it, trigger a fresh deploy so it's re-baked.

## 2. Backend (the missing half)

Deploy the blueprint at repo root — `render.yaml` provisions all of it in Ohio:
the `koel-voicebot` Docker **web service** (public), the `koel-stt` / `koel-nlp` /
`koel-tts` / `koel-crm` private services, managed **Postgres** (+pgvector) and
**Redis**. Or configure the web service by hand:

| Setting | Value |
|---|---|
| Type | Web Service · Docker |
| Dockerfile / context | `backend/Dockerfile` · `backend` |
| Start command | `uvicorn koel.voicebot.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health/ready` |
| Region | Ohio |

**Backend env (dashboard / synced group — never commit):**
- `KOEL_ENVIRONMENT=production`
- `KOEL_GROQ_API_KEY`, `KOEL_NVIDIA_API_KEY` — **rotate the keys shared in chat first**
- `KOEL_JWT_SECRET` — a strong secret (prod boot fails on the dev default)
- `KOEL_CORS_ALLOW_ORIGINS=https://koel-voice-bot.onrender.com` (the FE origin; no `*` in prod)
- `KOEL_DATABASE_URL` (from the managed Postgres), `KOEL_REDIS_URL` (from Redis — use `rediss://`)
- `KOEL_NLP_PROVIDER=groq`, `KOEL_STT_PROVIDER=groq`, `KOEL_TTS_PROVIDER=groq`, `KOEL_NLP_EMBEDDER=nvidia`
- `KOEL_STREAM_TTS=true` (sentence-streaming), `KOEL_TTS_HINDI_PROVIDER=nvidia`
- Inter-service URLs (private network): `KOEL_STT_SVC_URL=http://koel-stt:8101`, etc.

**One-time DB setup:** enable pgvector + apply migrations —
`alembic upgrade head` then seed with `python -m scripts.bootstrap_db` (a Render
Pre-Deploy command or One-Off Job).

## 3. Branch

Render is building **`main`**, but the latency + hardening work is on
`feat/groq-nlp-and-prd-hardening` (PR #1). Merge it (or point the service's branch
at it) before deploying, or you'll ship the pre-optimization build.

## 4. Free-tier caveats (both services)

- Free web services **spin down after ~15 min idle → ~50s cold start**; the first
  call after idle drops. Fine for a warm demo; use a paid **Starter** plan for
  always-on voice traffic.
- **512 MB RAM** is tight for the numpy ambient mixer + 5 uvicorn processes.
- WebSockets work on Render, but mind the idle timeout on long calls.

### Keeping it warm between demos

Two independent keep-alives (use either or both):

1. **In-app self-ping** — set `KOEL_KEEPALIVE_URL` to the backend's own public URL
   (e.g. `https://koel-backend-oj5p.onrender.com`). The combined app then pings
   `<url>/health` every `KOEL_KEEPALIVE_INTERVAL_SECONDS` (default 600s), which is
   inbound traffic that resets the idle timer. Keeps a **running** instance warm;
   can't wake one that already spun down.
2. **CI cron** — `.github/workflows/keepalive.yml` pings `/health` every 10 min and
   **also wakes a cold instance**. Scheduled workflows run only from the **default
   branch**, so this must be on `main` to tick; set repo variable `KEEPALIVE_URL`
   to override the target, or use the workflow's manual "Run" button before a demo.

> **Cost:** always-warm ≈ always-running ≈ **~730 of the 750 free instance-hours/mo**
> — fine for one demo service, but it can exhaust the monthly free allowance near
> month-end. To conserve, narrow the cron to demo hours (commented line in the
> workflow) and leave `KOEL_KEEPALIVE_URL` unset.

## 5. Verify after deploy

- Backend: `GET /health/ready` returns JSON `{"status":"ok","dependencies":{"postgres":true,"redis":true}}` (not HTML).
- Frontend: log in with `demo / koel-demo` → it should reach the backend (not fall to demo mode); a call opens `wss://<backend>/ws/calls/…`.
- Latency: run `backend/scripts/loadtest.py --base https://<backend-url>` for real first-audio / full-turn P95.
