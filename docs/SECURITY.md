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

- [ ] `KOEL_JWT_SECRET` set to a strong secret (prod boot fails on the dev default).
- [ ] `KOEL_CORS_ALLOW_ORIGINS` set to an explicit allowlist (no `*` in prod).
- [ ] Postgres `?ssl=require`, Redis `rediss://` + AUTH.
- [ ] Rotate the Groq + NVIDIA API keys that were shared in plaintext during dev.
- [ ] Rate-limit store moved to Redis for multi-worker deployments.
- [ ] Secrets sourced from a secret manager, not `.env`.
