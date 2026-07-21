# KOEL — Security Posture

Operational security controls and the production hardening required beyond dev.
Complements [RESIDENCY.md](RESIDENCY.md) (data-residency accepted risk).

## Transport encryption (N — required in production)

Dev runs plaintext on localhost. Production **must** use:

- **PostgreSQL:** append `?ssl=require` (asyncpg) to `KOEL_DATABASE_URL`, e.g.
  `postgresql+asyncpg://user:pass@rds-host:5432/koel?ssl=require`. Managed RDS
  enforces TLS; verify the CA in a hardened deployment.
- **Redis:** use a `rediss://` URL with AUTH, e.g.
  `KOEL_REDIS_URL=rediss://:<token>@elasticache-host:6379/0`. ElastiCache
  in-transit encryption + AUTH token.
- **Ingress:** terminate TLS at the load balancer; HSTS is already emitted by the
  security-headers middleware in production (see `common/app_factory.py`).

## PII handling

- **Logs:** name → initials, account/phone → last-4 (`common/logging.py`), applied
  at every business-event log site.
- **Before Groq egress (N):** `koel.common.text_guard.redact_pii` masks
  account/card/phone-length digit runs (8+ digits) in the customer turn **and** the
  CRM context before the NLP prompt is sent to the (US-hosted) Groq API. Names and
  short product figures (₹500 fee, 4x rewards) are preserved so intent/tone
  classification and grounding are unaffected.
- **Before TTS egress:** the same `redact_pii` runs on the bot's spoken reply in
  `orchestrator._send_bot_turn`, so an account number is never voiced/persisted.
- **DPDP data minimisation:** only allow-listed CRM attributes per use case are
  loaded into a session (`voicebot/api/sessions.py`).

## AuthN / AuthZ

- All REST endpoints require a JWT bearer token; the WS upgrade requires a valid
  short-lived (120s) `ws` token, validated **before** the socket accepts.
- **Access token TTL is short (900s, M);** the dashboard silently renews via
  `POST /v1/auth/refresh`. Reduces the blast radius of a leaked token.
- **WS token transport (M):** sent via the `Sec-WebSocket-Protocol` subprotocol
  (`koel.token.<jwt>`), not the URL query string, so it never lands in access
  logs / browser history. A deprecated `?token=` fallback remains for
  compatibility and should be disabled once all clients are updated.
- Service-principal role gates the internal AI/CRM services so a leaked operator
  token can't drive inference or probe the CRM.
- Session ownership: the WS token's `sub` must match the session owner — no
  attaching to another operator's live call.
- Tenant isolation (AF): `org_id` on the token scopes bot/call/KB/analytics reads;
  cross-tenant access returns 404 (no existence leak).
- **Known exception, accepted risk:** `POST /v1/users/invite` narrows but does not
  fully close cross-tenant enumeration — `users.username`/`users.email` are
  GLOBALLY unique at the DB level (a carry-over from the original single-tenant
  schema), so a tenant-scoped admin inviting a name that collides with another
  org's user still gets 409 (vs 201 for a genuinely free name), leaking one bit:
  "this identity exists somewhere on the platform" (never which org, never any
  other detail). Requires an already-authenticated `role=admin` principal and is
  rate-limited (`_credential_action_limit`/`_accept_limit`); there is no pre-auth
  or anonymous path to this signal. Closing it fully needs composite
  `(org_id, username)`/`(org_id, email)` unique constraints **plus** an
  org-disambiguation step added to `/v1/auth/login` (today's lookup is a bare
  `User.username == payload.username`, which assumes global uniqueness) — i.e. a
  login UX change for every user, not just admins. Explicitly accepted as
  disproportionate to the narrow, admin-only, rate-limited signal it would close;
  see the code-level detail in `koel/voicebot/api/users.py` (`invite_user`).

## Abuse controls (M)

- Per-IP sliding-window rate limits on `/v1/auth/login` (8/min),
  `/v1/auth/{refresh,ws-token}` (30/min), and `POST /v1/sessions` (20/min)
  via `koel.common.ratelimit`. **Single-worker scope** — a multi-worker
  deployment must swap in a shared store (slowapi + Redis) so the window is
  global, not per-process.
- Request field length caps (`Field(max_length=...)`) on id/credential/text inputs.

## Resilience (E)

- Circuit breaker + exponential-backoff-with-jitter (`koel.common.resilience`) on
  the Groq NLP call (retries 429/5xx/timeout, trips open after repeated failure).
- Filler warmup is parallel + per-phrase-timeout bounded so a cold provider can't
  stall boot.
- TTS routing cascades to the alternate engine on failure and flags `fallback_used`.

## Production checklist (not enforced in dev)

Two kinds of item below: **code-enforced** (the app itself refuses to boot, or
warns, if it's wrong — nothing to verify beyond actually setting the value)
and **operational** (a real action someone has to take outside this repo,
which no amount of code can do for you).

Code-enforced (`create_app`, `koel/common/app_factory.py`):
- [x] `KOEL_JWT_SECRET` set to a strong secret — **hard boot failure** in
      production on the dev default (`RuntimeError`, not just a warning).
- [x] `KOEL_CORS_ALLOW_ORIGINS` set to an explicit allowlist — **hard boot
      failure** in production on `*` or empty.
- [x] Rate-limit store on Redis for multi-worker deployments — already
      implemented (`RedisRateLimiter`, `koel/common/ratelimit.py`), selected via
      `KOEL_RATE_LIMIT_BACKEND=redis` (already set in `render.yaml`). Just
      confirm the env var is actually set in whatever deployment you're
      checking — the code path has existed since O-8.
- [x] `KOEL_SECRET_KEY` (BYOK Fernet key) — **now warns at boot** in
      production if unset/invalid (`warn_if_byok_secret_unset`), rather than
      staying silently dormant until the first BYOK API call fails. Still not
      a hard fail: BYOK is genuinely optional, an unrelated deployment
      shouldn't refuse to boot over it.

Operational (no code can verify or perform these for you):
- [ ] Postgres `?ssl=require`, Redis `rediss://` + AUTH — connection-string
      choices made at deploy time.
- [ ] Rotate every key shared in plaintext during dev: Groq, NVIDIA, Sarvam,
      Deepgram — plus the Render API token used to configure the deployment.
      (Groq/NVIDIA were flagged first; this is the complete list of what was
      actually pasted in-session — rotate all of it, not just those two.)
- [ ] `KOEL_SECRET_KEY` actually set in the `koel-secrets` env group — the
      boot-time warning above only fires if you deploy and check the logs;
      it doesn't set the value for you.
- [ ] Secrets sourced from a secret manager, not `.env` — a process/tooling
      choice for whoever owns the deployment, not something this codebase's
      boot sequence can enforce from the inside.
