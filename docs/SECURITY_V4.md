# KOEL — Security Posture v4

> Supersedes [SECURITY_V3.md](./SECURITY_V3.md) (v1/v2/v3 controls). v4 is the
> bug-hunt + telephony-parity + multi-tenant-admin bump: four rounds of
> whole-codebase review fixes (critical cross-tenant IDOR through telephony
> frame-parse hardening), Google Sign-In (allowlist model), self-service org/user
> provisioning, a newly-identified and explicitly accepted cross-tenant
> enumeration risk, and telephony calls reaching full parity with the browser
> transport on recording + persistence. Complements
> [RESIDENCY_V4.md](./RESIDENCY_V4.md).

## Control evolution (v1 → v2 → v3 → v4)

| Control | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Transport TLS (DB/Redis/ingress) | dev plaintext | documented (?ssl=require, rediss://, HSTS) | unchanged; Redis TLS now load-bearing (rate limiter) | unchanged |
| PII redaction (NLP + TTS egress) | — | redact_pii both paths | + applied per-sentence on the streaming path | unchanged |
| PII at rest in caches | n/a | n/a | TTS-cache TTL bounds derived-audio PII (O-3), configurable | unchanged |
| Call recording (audio at rest) | — | — | bot-audio only, WS/browser transport; telephony calls not persisted at all | + customer audio buffered too, on BOTH transports; telephony calls now persisted to Postgres at all (previously none were) |
| AuthN (JWT REST + 120s WS token) | check | check | check | + Google Sign-In (allowlist model): JWKS-verified ID token, returns the SAME 401 as a bad password for an unknown/unverified email — never reveals allowlist membership; rejects an email colliding with a non-Google row |
| Token TTL + silent refresh | design | /refresh built, FE gap | FE persists + silently refreshes (Q-2) | unchanged |
| Rate limiting | in-mem, single-worker | in-mem | Redis-backed (O-8), per-route namespace, login fail-closed | + Google-login namespace (10/min), fail-closed like password login |
| Tenant isolation (org_id seam) | soft | soft | enforced org scoping at login + per-row (6.4) | 4 bug-hunt batches closed residual cross-tenant IDORs (CRM `get_customer`/`list_customers`, interaction idempotency dedup, operator-claim authz on escalation reply/resolve); new org+user admin API enforces platform-vs-tenant-admin scoping (a tenant admin can never create/target a platform-admin row or another org's row) |
| Cross-tenant enumeration (`POST /v1/users/invite`) | n/a | n/a | n/a | identified + explicitly **accepted risk** — globally-unique `username`/`email` columns (single-tenant carry-over) leak one bit ("this identity exists somewhere on the platform") on a 409 vs 201; requires authenticated `role=admin`, rate-limited, no pre-auth path; closing it needs a login-UX change for every user, judged disproportionate |
| Secret storage | env only | env only | BYOK secrets encrypted at rest | + boot-time warning (not a hard fail) when `KOEL_SECRET_KEY` is unset/invalid in production |
| Outbound-request safety (SSRF) | n/a | n/a | SSRF guard on all BYOK provider URLs | closed a bypass: the guard only ran at connection create/update time — the live NLP/TTS/STT path re-read `base_url` verbatim; `resolve_connection` now re-validates on every resolve (off the event loop) |
| Security headers / CSP | middleware | middleware | unchanged | + production CORS that parses to empty (e.g. `",,"`) now fails closed instead of silently falling back to `*` |
| Telephony transport hardening | n/a | n/a | n/a | Twilio webhook signature verification fixed — it signed the URL without the query string, so every real inbound call would have failed verification; base64 media-frame decode and WAV-frame transcode now validate format and reject corrupt/non-16-bit-mono input instead of silently mis-decoding into garbled audio |

## v4 notes

- **Bug-hunt batches (4 rounds, all with regression tests, full suite green
  each time):** batch 1 fixed a CRITICAL cross-tenant PII IDOR (`get_customer`
  had no org filter) plus HIGH-severity SSRF, history-pagination, and CRM-dedup
  correctness bugs; batch 2 closed a WS finalize/data-loss cluster (calls ended
  out-of-band — smart-turn flush, streaming-STT final, abnormal telephony
  disconnect — could skip persistence/finalization entirely, losing a CallLog
  and leaking a Redis session to TTL); batch 3 fixed the remaining
  medium/low correctness + authz gaps (list_customers org filter, operator-claim
  authz on escalation actions, CORS fail-closed, unbounded knowledge-chunk
  length, blocking S3 upload inside a DB transaction); batch 3b hardened
  telephony frame parsing (dormant paths, defensive only — both callers already
  caught decode errors).
- **Human-escalation gating fix (part of the telephony-parity work):**
  previously the bot could keep talking over a customer already on hold for an
  operator on the telephony transport; escalation registration is now gated the
  same way it already was on the browser transport.
- **Cross-tenant enumeration** is new to the table, not new in the code — the
  gap itself is a carry-over from the original single-tenant schema; v4 is
  where it got a name, a rate-limit-based mitigation review, and an explicit
  accept-risk decision (see `docs/SECURITY.md` and `koel/voicebot/api/users.py`
  `invite_user`).
- **Admin surface growth:** `POST /v1/users/invite` (+ accept-invite,
  password-reset) and org provisioning are net-new authenticated admin API
  surface — reviewed for the same platform-vs-tenant scoping rules as the rest
  of the API, not just the list views.
- Credential-rotation checklist grew from Groq + NVIDIA (v3-era) to five items:
  **Groq, NVIDIA, Sarvam, Deepgram, and the Render deployment API token** — all
  pasted in plaintext during dev sessions. `docs/SECURITY.md`'s production
  checklist is now split into **code-enforced** (JWT secret, CORS allowlist,
  Redis rate-limit store, BYOK-key boot warning — `koel/common/app_factory.py`)
  and **operational** (TLS connection strings, the 5-key rotation, secret-manager
  migration) so it's clear which items the app itself refuses to boot without
  and which are standing human actions.

Mandatory production checklist from v3 (TLS everywhere, BYOK secrets encrypted
at rest, SSRF-guarded outbound requests) remains in force, now split as above;
rotate all 5 flagged keys before any real production traffic.
