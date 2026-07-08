# KOEL — Deploy (Render)

KOEL is **two deployables**: the React **frontend** (web service) and the FastAPI
**backend** (Docker web service, running as a single combined process). They
are deployed separately and pointed at each other. Region: **Ohio (US East)**
so the backend is co-located with Groq/NVIDIA (US-local inference, not
trans-Pacific) — Sarvam (India-hosted) is the exception, see
[`RESIDENCY.md`](./RESIDENCY.md).

> **Current state (live):** both the frontend
> (`koel-voice-bot.onrender.com`) and backend (`koel-backend-oj5p.onrender.com`)
> are deployed and wired together — the frontend is built with `VITE_KOEL_API`
> pointing at the real backend, and login/dashboard/calls/Knowledge Base all
> reach it live (verified end-to-end). The backend runs `koel.combined:app` —
> **all 5 services in one Python process**, chosen to fit the 512MB free-tier
> memory limit — and self-migrates its DB schema on boot (`_ensure_schema()`),
> since the free tier doesn't support pre-deploy hooks. **Note:** the backend
> service currently deploys from `main`; the frontend service currently
> deploys from `feat/groq-nlp-and-prd-hardening`. Merge the feature branch to
> `main` before relying on `main` alone reflecting the live frontend's code.

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

## 2. Backend (single combined process — what's actually deployed)

The live backend runs `koel.combined:app` — all 5 services (voicebot, stt, nlp,
tts, crm) in one process, needed to fit the 512MB free-tier memory limit.
`render.yaml` at repo root now describes exactly this combined single-service
topology (Blueprint deploy applies it directly); the independently-scalable
5-service split is preserved in `render.split.yaml` for the Phase-2 scale-out.
To configure the web service by hand instead, use:

| Setting | Value |
|---|---|
| Type | Web Service · Docker |
| Dockerfile / context | `backend/Dockerfile` · `backend` |
| Start command | `uvicorn koel.combined:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Region | Ohio |

**Backend env (dashboard / synced group — never commit):**
- `KOEL_ENVIRONMENT=production`
- `KOEL_GROQ_API_KEY`, `KOEL_NVIDIA_API_KEY`, `KOEL_SARVAM_API_KEY` — **rotate any keys ever shared outside a secret manager**
- `KOEL_JWT_SECRET` — a strong secret (prod boot fails on the dev default)
- `KOEL_CORS_ALLOW_ORIGINS=https://koel-voice-bot.onrender.com` (the FE origin; no `*` in prod)
- `KOEL_DATABASE_URL` (from the managed Postgres), `KOEL_REDIS_URL` (from Redis — use `rediss://`)
- `KOEL_NLP_PROVIDER=groq`, `KOEL_STT_PROVIDER=groq`, `KOEL_TTS_PROVIDER=groq`, `KOEL_NLP_EMBEDDER=nvidia`
- `KOEL_TTS_HINDI_PROVIDER=nvidia` (Sarvam auto-activates over this when `KOEL_SARVAM_API_KEY` is set — see [`LLD_V2.md`](./LLD_V2.md) §8.3)
- `KOEL_STREAM_TTS`, `KOEL_SARVAM_TTS_WS_ENABLED`, `KOEL_SMART_TURN_ENABLED` — all default to `true` in code; only set these to override
- `KOEL_KEEPALIVE_URL` — see §4 below
- Since everything runs in one process, the inter-service URLs point at `localhost`: `KOEL_STT_SVC_URL=http://localhost:$PORT/stt`, and likewise for `NLP_SVC_URL`/`TTS_SVC_URL`/`CRM_SVC_URL` under `/nlp`, `/tts`, `/crm`.

**DB setup:** the combined app **self-migrates and self-seeds on boot**
(`_ensure_schema()` in `koel/combined.py`) — it runs `alembic upgrade head` then
`scripts/bootstrap_db.py` automatically if the schema is missing or the seed
data is empty, because the free tier doesn't support pre-deploy hooks or
one-off jobs. No manual migration step is needed for this deployment shape.

## 3. Branch

The live backend tracks **`main`**; the live frontend tracks
`feat/groq-nlp-and-prd-hardening`. Merge the feature branch into `main`
regularly so both services are building from the same code — the two-branch
split is a known gap (tracked in `FUTURE_PROCEEDINGS.md`), not the intended
long-term state.

## 4. Free-tier caveats (both services)

- Free web services **spin down after ~15 min idle → ~50s cold start**; the first
  call after idle drops. Fine for a warm demo; use a paid **Starter** plan for
  always-on voice traffic.
- **512 MB RAM** is tight for the numpy ambient mixer + 5 uvicorn processes.
- WebSockets work on Render, but mind the idle timeout on long calls.

### Keeping it warm between demos

Two independent keep-alives (use either or both):

1. **In-app self-ping** — runs **only** under the single-process
   `koel.combined:app` entrypoint (what the free-tier box runs — the live backend's
   start command is `uvicorn koel.combined:app`). Set `KOEL_KEEPALIVE_URL` to the
   backend's own public URL (e.g. `https://koel-backend-oj5p.onrender.com`) and it
   pings `<url>/health` every `KOEL_KEEPALIVE_INTERVAL_SECONDS` (default 300s) —
   inbound traffic that resets the idle timer, with a fast retry after a failed
   ping so one blip can't open a >15-min gap. Keeps a **running** instance warm;
   can't wake one already spun down (the cron does). On a `starter` plan (the
   `render.yaml` default, and the `render.split.yaml` services) there's no
   spin-down, so this only matters on `free`.
2. **CI cron** — `.github/workflows/keepalive.yml` pings `/health` every 10 min and
   **also wakes a cold instance**. Scheduled workflows run only from the **default
   branch**, so this must be on `main` to tick; set repo variable `KEEPALIVE_URL`
   to override the target, or use the workflow's manual "Run" button before a demo.

> **Cost:** always-warm ≈ always-running ≈ **~730 of the 750 free instance-hours/mo**
> — fine for one demo service, but it can exhaust the monthly free allowance near
> month-end. To conserve, narrow the cron to demo hours (commented line in the
> workflow) and leave `KOEL_KEEPALIVE_URL` unset.

## 5. Verify after deploy

- Backend: `GET /health` returns `{"status":"ok",...}` (not HTML); `GET /health/ready` additionally reports `{"dependencies":{"postgres":true,"redis":true}}`.
- Logs: confirm `keepalive: self-ping …/health every …s` appears at boot (proves `KOEL_KEEPALIVE_URL` took effect on this deploy — a `PUT` to an env var during an in-flight deploy can miss it; trigger a fresh deploy if the log line is absent).
- Frontend: log in with `demo / koel-demo` → it should reach the backend live (dashboard stats change between reloads, not canned numbers); a call opens `wss://<backend>/ws/calls/…` and produces real (non-silent) audio.
- Latency: run `backend/scripts/loadtest.py --sessions 10 --turns 3 --base https://<backend-url>` for a real first-audio P50/P95/P99 — see [`SLO.md`](./SLO.md) for the last measured baseline and why &lt;300ms full-turn isn't the right bar to check it against.
